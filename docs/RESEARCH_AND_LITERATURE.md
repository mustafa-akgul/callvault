# Araştırma, Literatür ve Teknoloji Kıyaslaması

Bu belge dört teknik kararın (STT, multi-tenancy, kuyruk/durum makinesi, Türkçe PII maskeleme) arka planını ve mimariye yönelik karşıt tezleri (steelman) içerir. Kararların kendisi `DECISIONS.md`'de.

**Doğrulama notu.** Sağlayıcı limitleri ve benchmark sayıları değişkendir. "Doğrulandı" diye işaretlenenler 2026-11-08'de kaynağından okundu. "Ölçülmeli" diye işaretlenenler ise bu veri setinde henüz ölçülmedi; uygulama aşamasında ölçülüp bu belge güncellenecek.

---

## 1. Speech-to-Text: faster-whisper (yerel) vs Groq API (barındırılan)

### 1.1 Arka plan

- **Whisper** (Radford ve ark., 2022, *Robust Speech Recognition via Large-Scale Weak Supervision*): 680 bin saatlik zayıf denetimli veriyle eğitilmiş, çok dilli bir encoder-decoder modeli. Türkçe, eğitim verisinde orta-yüksek kaynaklı diller arasında.
- **faster-whisper**: Whisper'ın CTranslate2 ile yeniden gerçeklenmiş hâli. Projenin kendi benchmark'larına göre aynı doğrulukta, referans gerçeklemeden ~4 kat hızlı ve daha az bellek kullanıyor; CPU'da int8 nicemleme destekliyor. Silero VAD filtresi dahili.
- **Groq**: Whisper large-v3 ve large-v3-turbo'yu kendi LPU donanımında, OpenAI uyumlu `/audio/transcriptions` uç noktasıyla sunuyor.

### 1.2 Türkçe doğruluk (literatür)

Bağımsız ve aynı normalizasyonla ölçülmüş bir large-v3 Türkçe WER değeri bulunamadı. Mevcut sayılar farklı test bölümleri ve normalizasyonlarla ölçülmüş:

| Kaynak | Model | Veri | Metrik | Değer | Güvenilirlik |
|---|---|---|---|---|---|
| BuzzASR model kartı (Hugging Face) | large-v3 sıfır-atış | FLEURS-tr | CER | 2,42 % | Topluluk; CER, WER ile karşılaştırılamaz |
| Aynı kaynak | large-v3 sıfır-atış | Common Voice 25 tr | CER | 5,15 % | Topluluk |
| ElevenLabs Türkçe sayfası | large-v3 | FLEURS | WER | 7,5 % | Rakip pazarlama sayfası, yöntem belirtilmemiş |
| selimc/whisper-large-v3-turbo-turkish | large-v3-turbo | MediaSpeech tr | WER | 12,32 % | Topluluk, özel normalizasyon |

**Çıkarım.** FLEURS okuma konuşmasıdır ve 16 kHz'dir. Bizim verimiz **8 kHz telefon** kaydı ve spontan konuşma, bu yüzden gerçek WER büyük olasılıkla bu sayılardan belirgin biçimde yüksek olacak (ölçülmeli). Asıl risk genel WER değil, **sayıların ve özel adların** doğru yazılması: IBAN, telefon, TCKN, saat. Bu hem PII maskelemeyi hem slot çıkarımını doğrudan etkiliyor.

### 1.3 Kıyaslama

| Kriter | faster-whisper (yerel, large-v3, CPU int8) | Groq API (`whisper-large-v3`) |
|---|---|---|
| Model | Aynı ağırlıklar | Aynı ağırlıklar (Groq'ta turbo seçeneği de var) |
| Maliyet | Ücretsiz | Ücretsiz katman |
| Limitler | Donanıma bağlı | Ücretsiz katman: saatte 7.200 ses-saniyesi, günde 28.800; 20 istek/dk, 2.000 istek/gün; dosya ≤ 25 MB (doğrulandı, kaynaklar arasında küçük çelişkiler var, konsoldan kontrol edilmeli). İstek başına en az 10 sn faturalanıyor |
| Veri setine uyum | ≈3.300 sn ses. CPU'da large-v3 yaklaşık gerçek zamanlı çalışır, yani tüm set için ≈30–60 dk (ölçülmeli) | Kanal başına transkripsiyonla ≈6.600 ses-sn, saatlik kotaya sığar ama yeniden denemelerle sınırda. En büyük dosya 13,5 MB (sınırın altında) |
| Temiz makinede kurulum | ~3 GB model indirmesi, büyük Docker imajı, ilk çalıştırma yavaş | Yalnız bir API anahtarı |
| Hata modları | OOM, yavaşlık; anahtar yok | 401 (anahtar), 429 (kota), 5xx, ağ hataları |
| "STT anahtarını bozacağız" testi | Bozulacak bir anahtar yok. Değerlendiricinin senaryosu uygulanamaz, kaçamak gibi görünür | Tam olarak test edilen senaryo |
| Gizlilik | Ses makineden çıkmaz (en güçlü seçenek) | Ham ses (PII içerir) üçüncü tarafa gider. Groq'un veri saklama politikası üretimden önce okunmalı |
| Determinizm | `temperature=0` + sabit sürümle yüksek | Sağlayıcı sürüm değiştirebilir |

### 1.4 Alternatifler (kısaca)

- **whisper.cpp**: CPU'da verimli, ama Python dışı bir bağımlılık ekliyor; faster-whisper ile aynı nişte.
- **ElevenLabs Scribe**: `.env.example` bunu ima ediyor (D11). Türkçe için iddialı sonuçlar veriyor ama ücretsiz katman dakika olarak küçük; değerlendiricinin kendi anahtarıyla çalıştırması gerekir.
- **Deepgram / AssemblyAI / Soniox**: Deneme kredili. Türkçe desteği ve kalitesi modele göre değişiyor; deneme kredisi tükenme riski var.

### 1.5 Ön işleme bulguları

- Kanallar konuşmacı bazında ayrık. Her kanalı ayrı mono dosya olarak transkribe edip segment zaman damgalarıyla birleştirmek, ek bir model olmadan diarizasyon sağlar. Bedeli 2 kat ses-saniyesi.
- 8 kHz'den 16 kHz'e çevirme Whisper'ın kendi ön işlemesinde zaten yapılıyor; doğruluk kazandırmaz. FLAC'a çevirmek yükleme boyutunu küçültür.
- kesik örnek kayıt bozuk: STT'ye gönderilmeden önce yakalanmalı. Aksi hâlde Whisper sessizlikte **halüsinasyon** üretebilir. Bu, Whisper'ın literatürde iyi bilinen bir davranışı (Koenecke ve ark., 2024, *Careless Whisper*).

---

## 2. Multi-tenancy: PostgreSQL RLS vs şema-başına-tenant vs sütun filtreleme

### 2.1 Literatür / desenler

- Sınıflandırma (Microsoft *Multi-tenant SaaS database tenancy patterns*; AWS *SaaS Lens*): **silo** (tenant başına veritabanı), **bridge** (tenant başına şema), **pool** (ortak tablolar + `tenant_id`).
- PostgreSQL Row-Level Security (sürüm 9.5'ten beri): `CREATE POLICY ... USING (...) WITH CHECK (...)`. Tablo sahibi ve `BYPASSRLS` yetkili roller politikayı atlar, **`FORCE ROW LEVEL SECURITY` ise sahibi de kapsar**. Superuser her zaman atlar. (PostgreSQL docs, *Row Security Policies*.)
- AWS'nin "Multi-tenant data isolation with PostgreSQL Row Level Security" yazısı ve Citus/Crunchy Data yazıları aynı deseni öneriyor: oturum değişkeni (`SET app.tenant_id`) + politika.

### 2.2 Kıyaslama

| Kriter | Sütun filtreleme (yalnız uygulama `WHERE tenant_id=`) | Şema-başına-tenant | Pool + RLS (FORCE) |
|---|---|---|---|
| İzolasyonun yeri | Uygulama kodu (her sorgu) | DB namespace + `search_path` | DB politikası |
| "Kazara bozmak" ne kadar kolay | **Çok kolay**: unutulan tek bir `WHERE` sızıntı demek | Orta: yanlış `search_path` veya havuzda kalan `SET` başka tenant'ı gösterir | Zor: unutulan `WHERE` yine filtrelenir. Bağlam yoksa fail-closed hata verir |
| Bunu yakalayan test | Her sorguya özel test gerekir | Şema listesi testi | Katalog testi: her tenant tablosunda RLS zorunlu (NFR-SEC-06) |
| Migrasyon | Tek | N şema × migrasyon (yavaş, kısmi başarısızlık riski) | Tek |
| Ölçek | İyi | Yüzlerce şemaya kadar makul, binlerde katalog şişer | İyi; `tenant_id` öncelikli bileşik indeks gerekir |
| Tenant'lar arası kuyruk | Kolay | Zor (worker her şemayı taramalı) | Kolay (worker, sistem rolüyle yalnız iş tablosunu okur) |
| Performans | En iyi | İyi | Politika ifadesi basit tutulursa iyi. `current_setting` STABLE olduğu için planlayıcı onu sabit gibi kullanabilir |
| Bilinen tuzaklar | — | Havuzda `SET search_path` sızıntısı | `SET` (LOCAL değil) havuzda sızar. Tablo sahibi rol olarak bağlanmak RLS'i devre dışı bırakır. `SECURITY DEFINER` fonksiyonlar ve view'lar (PG15 öncesi `security_invoker` yok) RLS'i atlatabilir |

### 2.3 Sonuç

Pool + RLS (FORCE) + ayrık roller (`app_owner` migrasyon, `app_user` uygulama, `app_worker` kuyruk) seçildi. Gerekçe ve bedel D-02'de. Önemli ek savunma: **bileşik yabancı anahtarlar** `(tenant_id, recording_id)`. RLS okumayı filtreler, ama FK kontrolleri RLS'i atlar. Bileşik FK, bir tenant'ın satırının başka tenant'ın kaydına bağlanmasını şemada imkânsız kılar.

---

## 3. Kuyruk ve durum makinesi: Celery, ARQ, BullMQ, Redis Streams, Postgres-as-queue

### 3.1 Gereksinimlerin kuyruğa yüklediği yük

1. Worker SIGKILL ile öldürülünce iş kaybolmamalı (FR-20). Bu at-least-once teslimat ve ack'in iş bittikten sonra yapılması demek.
2. İki worker aynı işi aynı anda yapmamalı (FR-21). Bu kiralama (lease) ve fencing gerektirir.
3. Durum her an görünür olmalı ve "başarısız adımdan devam" edilebilmeli (FR-16, FR-19). Durum kalıcı olmalı ve iş ile tutarlı olmalı.
4. Yan etkiler idempotent olmalı (FR-22). Bu kuyruktan bağımsız olarak uygulama katmanında çözülür.

**Temel literatür.** Tam-bir-kez *teslimat* dağıtık sistemlerde mümkün değildir. Ulaşılabilecek olan, en-az-bir-kez teslimat ile idempotent işlemenin birleşimidir (Kleppmann, *Designing Data-Intensive Applications*, böl. 11; Helland, *Life beyond Distributed Transactions*, 2007). Kuyruk ve veritabanının ayrı sistemler olduğu durumda **çift yazma (dual write)** sorununun çözümü *Transactional Outbox* desenidir (Richardson, *Microservices Patterns*).

### 3.2 Kıyaslama

| Kriter | Celery + Redis | ARQ (Redis, asyncio) | BullMQ (Redis, Node.js) | Postgres tablosu + `FOR UPDATE SKIP LOCKED` (ör. kendi gerçeklemesi veya Procrastinate) |
|---|---|---|---|---|
| SIGKILL'de iş kaybı | Varsayılan `acks_late=False` ile **kaybolur**. `acks_late=True` + `task_reject_on_worker_lost=True` gerekir. Redis broker'da iş `visibility_timeout` (varsayılan 1 saat) dolana kadar bekler | İş, `in-progress` anahtarının süresi dolunca yeniden alınır; at-least-once. Kurtarma süresi `job_timeout`'a bağlı | Kilit yenileme + "stalled job" tespiti ile yeniden kuyruğa alınır; olgun bir mekanizma | Kira (`locked_until`) dolunca başka worker alır; heartbeat ile uzatılır |
| Çift işleme riski | `visibility_timeout`'tan uzun süren işler **ikinci bir worker'a teslim edilir** (bilinen Celery/Kombu davranışı). ETA/countdown görevlerinde de aynı sorun var | Zaman aşımı yanlış ayarlanırsa var | Yavaş işlerde stalled olarak işaretlenip çift çalışabilir (`maxStalledCount`) | Kira süresi heartbeat'ten kısa ayarlanırsa var. Fencing token ile zombi worker'ın yazması reddedilir |
| Durum görünürlüğü | Sonuç backend'i ayrı; durum DB ile kuyruk arasında **iki yerde** | Redis'te, kısa ömürlü | Redis'te, iyi bir UI'si var (Bull Board) | Durum ve iş **aynı transaction'da**; SQL ile sorgulanabilir |
| Çift yazma | Var: "DB'ye durum yaz + kuyruğa iş koy" atomik değil, outbox gerekir | Var | Var | **Yok** |
| "Başarısız adımdan devam" | Uygulama mantığı (chain yeniden kurulur) | Uygulama mantığı | Flow'lar var ama durum Redis'te | Doğal: adım tablosundan ilk başarısız adım seçilir |
| Ek altyapı | Redis + Celery | Redis | Redis + Node runtime (Python yığınıyla iki dil) | Yok (zaten Postgres var) |
| Ölçek tavanı | Çok yüksek | Yüksek | Çok yüksek | Orta: saniyede yüzlerce-binlerce iş. Bizim yükümüz (21 kayıt, CRM 2 rps) bunun çok altında |
| Rate limit (CRM 2 rps global) | Görev başına `rate_limit`, ama worker başına çalışıyor, global değil | Yok, kendin yazarsın | Kuyruk seviyesinde global limiter (güçlü yanı) | Advisory lock veya token tablosuyla global; kendin yazarsın |

**Redis Streams** (consumer group + `XAUTOCLAIM`) de at-least-once ve sahiplenme sağlar, ama durum yine Redis'te kalır ve çift yazma problemi sürer.

### 3.3 Durum makinesi modeli

```
PENDING → VALIDATING → TRANSCRIBING → MASKING → EXTRACTING → CRM_SYNC → DONE
                \             \            \           \            \
                 └─────────────┴────────────┴───────────┴────────────┴→ FAILED(step, error_code, error_detail)
CRM_SYNC alt-durumları (aksiyon başına, outbox satırı):
  meeting:   PLANNED → SLOT_CHOSEN(key) → SENT? → BOOKED | NEEDS_FOLLOWUP | FAILED
  complaint: PLANNED(key) → SENT? → CREATED | FAILED
Ek terminal: DONE(duplicate_of=rec_x)
```

- Her adımın çıktısı kalıcıdır (transkript, maskeli metin, çıkarım JSON'u). Bir sonraki adım girdisini DB'den okur. Bu sayede "başarısız adımdan devam" önceki adımları tekrar etmeden mümkün olur.
- Geçişler koşullu `UPDATE ... WHERE status = $expected AND lease_token = $mine` ile yapılır. Bu iyimser eşzamanlılık + fencing sağlar.
- Hata sınıfları: `transient` (5xx, 429, zaman aşımı, bağlantı kopması → geri çekilerek yeniden dene), `permanent` (401/403, bozuk ses, 422 → hemen FAILED, `reprocess` ile elle devam), `unknown_outcome` (yazma isteğinde bağlantı kopması → aynı anahtarla yeniden dene, asla yeni anahtar üretme).

---

## 4. Türkçe PII maskeleme

### 4.1 Varlık türleri ve deterministik doğrulayıcılar

| Tür | Yapı | Doğrulayıcı | Kaynak |
|---|---|---|---|
| TCKN | 11 hane, ilk hane ≠ 0 | 10. hane = ((d1+d3+d5+d7+d9)·7 − (d2+d4+d6+d8)) mod 10; 11. hane = (d1..d10 toplamı) mod 10 | NVİ kamuya açık algoritma |
| IBAN (TR) | `TR` + 2 kontrol + 5 banka kodu + 1 rezerv (0) + 16 hesap = 26 karakter | ISO 13616 / ISO 7064 mod-97 = 1 | ISO 13616 |
| Kart | 13–19 hane (genelde 16), BIN önekleri | Luhn (ISO/IEC 7812-1) | ISO/IEC 7812 |
| Telefon | `+90`/`0` + 10 hane; mobil `5xx` | Uzunluk + önek | BTK numaralandırma planı |
| E-posta | RFC 5322'nin pratik alt kümesi | Regex; STT çıktısında "at", "et", "nokta" kelimeleri normalize edilir | — |
| Ad-soyad | Açık sınıf | Doğrulayıcı yok; NER + bağlam | — |

### 4.2 STT'ye özgü zorluklar (literatürde az işlenmiş, bu proje için kritik)

1. **Sayıların bölünmesi.** Konuşmacı numarayı gruplar hâlinde söyler ("beş yüz otuz iki, kırk beş, altmış yedi"). Whisper bunu bazen rakamla, bazen kelimeyle, bazen karışık yazar. Regex'ten önce **sayı normalizasyonu** (Türkçe sayı kelimesi → rakam) ve boşluk/tire/nokta ile ayrılmış rakam gruplarının birleştirilmesi gerekir.
2. **Checksum'ın iki yüzü.** STT bir haneyi yanlış duyarsa gerçek bir TCKN checksum'dan geçemez. Yalnız checksum'a güvenmek recall kaybettirir. Karar: *bağlam* varsa ("kimlik numaram", "TC", "IBAN") uzunluğa uyan her rakam dizisi checksum'dan bağımsız maskelenir. Bağlam yoksa checksum gerekir.
3. **Ad belirsizliği.** Türkçe adların önemli bir kısmı aynı zamanda ortak kelimedir (Umut, Deniz, Barış, Can, Su). NER tek başına hem yanlış pozitif hem yanlış negatif üretir. Bağlam kalıpları ("adım X", "ben X", "X Bey/Hanım", temsilcinin "X Hanım, ...") güçlü sinyallerdir.
4. **Kesme işareti ve ekler.** "Ahmet'in", "Yılmaz'a" gibi biçimlerde kesme işaretinden sonraki ek korunup kök maskelenmeli (`[AD]'in`). STT kesme işaretini atlayabilir ("Ahmetin"), bu durumda NER span'ı tüm token'ı kapsamalı.
5. **Maskelemenin zamanı bozmaması.** "Saat on beş otuz", "3 Kasım", "14:00" gibi ifadeler telefon/kart regex'lerine takılmamalı (negatif test kümesi, NFR-ACC-03).

### 4.3 Yöntem kıyaslaması

| Yöntem | Güçlü | Zayıf | Türkçe durumu |
|---|---|---|---|
| Saf regex + doğrulayıcı | Deterministik, açıklanabilir, hızlı, yapılandırılmış PII'de yüksek kesinlik | Adları yakalamaz; STT varyasyonlarına kırılgan | Dil bağımsız, kurallar yazılmalı |
| Microsoft Presidio | Tanıyıcı (recognizer) çerçevesi, bağlam kelimesiyle puan artırma, anonimleştirici operatörleri | Hazır Türkçe NLP modeli yok; spaCy'nin resmî Türkçe pipeline'ı yok | Özel tanıyıcılar + topluluk spaCy modeli (turkish-nlp-suite `tr_core_news_*`) veya HF NER ile entegre edilebilir |
| Türkçe BERT NER (ör. `savasy/bert-base-turkish-ner-cased`, `akdeniz27/xlm-roberta-base-turkish-ner`; WikiANN/Türkçe NER veri setleri üzerinde eğitilmiş) | Adlarda iyi; yerel çalışır | Haber metninde eğitilmiş, konuşma dilinde ve küçük harfli STT çıktısında başarı düşer (ölçülmeli); ~500 MB model | Mevcut |
| LLM tabanlı (ör. Groq üzerinde Llama 3.x 70B) | Bağlamı anlar, sayı kelimelerini çözer | Deterministik değil, halüsinasyon (metni değiştirebilir); **ham PII'yi üçüncü tarafa göndermek** gizlilik açısından çelişkili | İyi |
| Hibrit (seçilen) | Yapılandırılmış PII için deterministik; adlar için NER + bağlam; LLM yalnız ikinci görüş olarak ve *maskeli* metinle | Daha fazla bileşen | — |

**Değerlendirme yöntemi.** Bu veri seti için elle etiketlenmiş küçük bir altın küme (her türden en az 5 pozitif ve zaman/sayı içeren negatif örnek) hazırlanacak. Tür başına recall ve kesinlik raporlanacak. Hedef: yapılandırılmış PII'de recall'ı önceliklendirmek (sızıntı, aşırı maskelemeden daha pahalı).

**Saklama modeli.** Maskeli metin `[AD_1]`, `[TELEFON_1]` gibi indeksli yer tutucular içerir (aynı varlık aynı indeksi alır, metin okunabilir kalır). Ham metin ve varlık-değer eşlemesi şifreli olarak ayrı tabloda tutulur. Bu sayede maskeleyici iyileştirildiğinde STT tekrarlanmadan maskeleme yeniden çalıştırılabilir.

---

## 5. Karşıt tezler (steelman): mimariye yönelik en güçlü saldırılar

Her başlıkta önce saldırı, mümkün olan en güçlü hâliyle veriliyor. Ardından savunma ve kalan risk geliyor. Risk kimlikleri (R-xx) DECISIONS.md'de kullanılıyor.

### R-01. Kayıp cevap + slot yeniden seçimi = sessiz mükerrer rezervasyon (en tehlikeli senaryo)

**Saldırı.** Worker, çağrı A için slot A'yı `POST /bookings` ile gönderir. CRM yazar ve kaydeder, sonra bağlantıyı keser. Worker bunu ağ hatası olarak görür. Yeniden denemeden önce "güncel olsun" diye `GET /slots` çağırır. A artık *dolu* görünür, çünkü kendi rezervasyonumuz doldurdu. Algoritma "istenen slot dolu → sonraki en erken slot" kuralını uygular, B'yi yeni bir anahtarla gönderir, 201 alır. **Aynı çağrı için iki rezervasyon** oluşur. Her iki HTTP çağrısı da "doğru" davranmıştır ve idempotency anahtarları da çalışmıştır. Hata tasarım seviyesindedir. Anahtar `uuid4()` ile bellekte üretilip gönderimden önce kalıcılaştırılmazsa, worker o anda SIGKILL ile öldürüldüğünde de aynı sonuç oluşur.

**Savunma.**
1. **Niyet gönderimden önce kalıcılaştırılır.** Slot seçimi ve anahtar (`booking_attempts` satırı: slot, anahtar, gövde) `POST`'tan önce commit edilir.
2. **Bilinmeyen sonuçta asla yeni slot seçilmez.** Bağlantı koparsa, zaman aşımı olursa ya da 5xx/429 dönerse *aynı anahtar ve aynı gövde* yeniden gönderilir. CRM ya replay 201 döner ya da (yazma hiç olmadıysa) normal işler. Yeni slot yalnız **kesin 409** cevabından sonra seçilir. 409 da idempotency kaydına yazıldığı için replay'de yine 409 döner, tutarlıdır.
3. **Uzlaştırma.** Herhangi bir rezervasyon denemesinden önce chaos'tan etkilenmeyen `GET /bookings` (D10) okunur. `call_id` bu kayda ait bir rezervasyon varsa o benimsenir ve yeni gönderim yapılmaz. Bu, kalıcılaştırma ile gönderim arasındaki en dar pencereyi de kapatır.

**Kalan risk.** Uzlaştırma `call_id` eşleşmesine dayanıyor. Gerçek bir CRM'de bu alan tenant'lar arası benzersiz olmayabilir, bkz. R-07. Gerçek bir CRM'de `GET` uç noktası da güvenilir olmayabilir. Bu durumda yalnız (1) ve (2) kalır ve yine yeterlidir; (3) derinlemesine savunmadır.

### R-02. Worker SIGKILL: yan etki yapıldı, durum yazılmadı

**Saldırı.** Worker CRM'e şikâyeti gönderir, 201 alır ve tam `UPDATE status='CREATED'` öncesinde SIGKILL alır. Kira dolar, ikinci worker işi alır ve şikâyeti *yeniden* gönderir. Adım idempotent değilse mükerrer şikâyet oluşur. STT tarafında da aynı şey olur: transkripsiyon parası/kotası iki kez harcanır. Bu küçük bir maliyettir ama Groq kotasını tüketerek diğer kayıtları `FAILED` yapabilir.

**Savunma.** Şikâyet anahtarı deterministiktir: `uuid5(tenant, recording, "complaint")`. Gövde (özet) bir önceki adımda üretilip **kalıcılaştırıldığı** için ikinci gönderimde birebir aynıdır. Özet her denemede LLM'den yeniden üretilseydi gövde değişir, CRM 422 döner ve kayıt sonsuza kadar takılırdı. Bu ayrıntı kritik. STT için: transkript commit edilmeden adım tamamlanmış sayılmaz. Çift harcama mümkün ama sınırlıdır (en fazla bir ek çağrı).

**Kalan risk.** Kira süresi, STT çağrısının en uzun süresinden (en uzun kayıt, 422 sn ses) kısa ayarlanırsa canlı bir worker'ın işi ikinci bir worker'a geçer, yani çift işleme olur. Önlem: heartbeat (her 10 sn'de `locked_until` uzatılır) ve fencing (bitişte `lease_token` kontrolü; zombi worker'ın yazması 0 satır günceller ve atılır).

### R-03. Yarış: iki worker, iki farklı kayıt, aynı slot

**Saldırı.** çağrı A ve çağrı B aynı anda "3 Kasım 15:00" ister. İkisi de `GET /slots` ile slotu boş görür ve ikisi de gönderir. Biri 201, diğeri 409 alır. Sonuç doğru görünür. Ama 409 alan kayıt, "sonraki en erken slot" için *eski* slot listesini kullanırsa, o anda üçüncü bir worker'ın aldığı slotu seçip art arda 409 zinciri yaşar. Her 409 ayrı bir CRM isteği ve rate limit 2 rps olduğu için 429 fırtınası oluşur.

**Savunma.** 409'dan sonra slot listesi yeniden okunur; reddedilen slotlar yerel dışlama kümesine eklenir. Gerçek tek-yazıcı garantisi CRM'in kendisidir (slot benzersizliği). Biz yalnız kararlı ve sonlu bir yeniden deneme zinciri sağlıyoruz: en fazla N slot denenir, sonra `needs_followup`. Bir seçenek de rezervasyon adımını tenant başına tek bir mantıksal kanalda (advisory lock) seri yapmak. 2 rps limiti zaten paralelliği anlamsız kıldığı için bu ucuz bir seçenek (D-06).

**Kalan risk.** Sıra bağımlılığı: "kim önce isterse slotu alır" kuralı worker zamanlamasına bağlıdır, yani **sonuç deterministik değildir**. İki çağrı aynı slotu istediğinde hangisinin kazanacağını `recorded_at` sırası belirlemeli mi? İş kuralı açısından mantıklı olan bu (önce arayan kazanır). Bunun için rezervasyonlar `recorded_at` sırasıyla seri işlenir (D-06). Bedeli, rezervasyon adımında paralellik olmaması.

### R-04. Mükerrer *kayıt* (kopya örnek kayıt = ilk kayıt)

**Saldırı.** Sistem her `recording_id` için kusursuz idempotenttir, ama kopya örnek kayıt ilk kayıt'in byte-düzeyinde kopyasıdır. İki ayrı çağrı olarak işlenirse aynı müşteri için iki toplantı ve iki şikâyet oluşur. Değerlendirici CRM'de "duplicates" ararken bunu görür. Tersi de bir saldırı: kopya örnek kayıt'i atlamak "silently skipped" ihlalidir.

**Savunma.** Tenant içinde `audio_sha256` ile tespit edilir. kopya örnek kayıt transkribe edilmez; transkript bağlantısı kurulur ve `DONE(duplicate_of=ilk kayıt)` durumuna geçer. CRM aksiyonu açılmaz. Durum ve gerekçe API'de görünür. Bu bir *varsayımdır* ve DECISIONS.md'ye yazıldı.

**Kalan risk.** Yeniden kodlanmış veya kırpılmış kopyalar (byte farklı, ses aynı) yakalanmaz. İçerik düzeyinde tekilleştirme (transkript benzerliği + arayan kimliği + istenen zaman) yanlış pozitif riski taşır; kapsam dışı.

### R-05. Kiracı sızıntısı: RLS "var" ama atlanıyor

**Saldırılar (her biri gerçek dünyada yaşanmış sınıflar):**
1. Uygulama tablo sahibi rolle bağlanır. `FORCE` yoksa RLS **hiç uygulanmaz**, tüm testler yeşil kalır ama hiçbir şey test edilmemiş olur. Migrasyon ve uygulama genellikle aynı `DATABASE_URL`'i kullandığı için bu en olası hata.
2. `SET app.tenant_id` (LOCAL değil) havuzdaki bağlantıda kalır. Bir sonraki istek, tenant ayarlamayı unutursa önceki tenant'ın verisini görür.
3. Politikada `current_setting('app.tenant_id', true)` (missing_ok) kullanılırsa bağlam yokken NULL döner. `tenant_id = NULL` boş sonuç verir. Bu "güvenli" görünür, ama yazmada `WITH CHECK` eksikse NULL veya yanlış tenant'lı satır eklenebilir.
4. Worker'ın tenant'lar arası iş tablosunu okuyabilmesi için `BYPASSRLS` verilir. Aynı rol transkriptleri de okur, sızıntı yüzeyi büyür.
5. `SECURITY DEFINER` fonksiyon veya view (PG < 15'te `security_invoker` yok) RLS'i sessizce atlar.
6. FK kontrolleri RLS'e tabi değildir. `acme` tenant'ının bir `complaint` satırı harbor'un `recording_id`'sine bağlanabilir.
7. Uygulama dışı kanal: CRM idempotency anahtarları global. `acme` de `ilk kayıt` adında bir kayda sahipse ve anahtar `booking:ilk kayıt` ise CRM, **harbor'un rezervasyon cevabını acme'ye replay eder**. Bu, DB'nin tamamen dışında bir sızıntı.
8. Loglar ve hata mesajları: "recording çağrı C belongs to tenant harbor" gibi bir 403 mesajı varlık sızdırır.

**Savunmalar.** (1) Ayrı roller; `app_user` NOSUPERUSER NOBYPASSRLS ve tablo sahibi değil. `FORCE` de açık. Test, `current_user`'ın tablo sahibi olmadığını ve RLS bayraklarını katalogdan doğrular. (2) Yalnız `SET LOCAL`; tenant'lı transaction açan tek bir context manager; havuz yeniden kullanım testi. (3) `missing_ok=false` (fail-closed) + `WITH CHECK`. (4) `app_worker` rolü yalnız `jobs` tablosunu tenant'sız okur. İş alındıktan sonra işin `tenant_id`'si ile `SET LOCAL` yapılır ve geri kalan her şey RLS altında çalışır. (5) `SECURITY DEFINER` yasak; view'lar `security_invoker=true` (PG16). (6) Bileşik FK. (7) Anahtar `uuid5` ile tenant ad-alanlı (NFR-SEC-12). (8) Başka tenant'ın kaynağına 404.

**Kalan risk.** `app_owner` ile bağlanan bir migrasyon veya bakım betiği hâlâ her şeyi görebilir. Bu operasyonel bir kontrol (kimlik bilgisi ayrımı), kodla tamamen çözülemez.

### R-06. Postgres-as-queue'ya karşı: "Neden olgun bir kuyruk değil?"

**Saldırı.** Kendi kuyruğunu yazmak tekerleği yeniden icat etmektir. Kira, heartbeat, geri çekilme, ölü mektup kuyruğu (DLQ), zamanlanmış yeniden deneme gibi mekanizmaların hepsi Celery/BullMQ'da test edilmiş durumda. Kendi gerçeklemende ince yarış hataları olur. `SKIP LOCKED` ile yoklama (polling), DB'ye yük bindirir ve gecikme ekler. Yüksek hacimde tablo şişer (MVCC ölü satırları, autovacuum baskısı).

**Savunma.** Gereksinimlerin özü (kalıcı durum, adımdan devam, durum ile işin atomikliği) olgun kuyruklarda da uygulama tarafında yeniden yazılmak zorunda. Celery'nin kendisi bunu sağlamıyor, üstelik çift yazma ekliyor. Bizim hacmimizde (21 kayıt; 2 rps CRM) yoklama yükü önemsiz. `LISTEN/NOTIFY` ile gecikme düşürülebilir. Kod küçük (~150 satır) ve SIGKILL testleriyle doğrulanıyor. Olgun bir alternatif olarak **Procrastinate** (Postgres tabanlı Python kuyruğu) not edildi.

**Ne zaman değiştirilir.** Saniyede binlerce iş, çoklu servis tüketicileri veya Postgres'in tek nokta hatası olmasının kabul edilemez hâle gelmesi durumunda.

### R-07. Tenant başına CRM varsayımı

**Saldırı.** Mock tek bir CRM. `call_id` tenant içermiyor. Gerçek ürün ya tenant başına ayrı CRM kullanır (o zaman sorun yok) ya da ortak bir CRM (o zaman `call_id` çakışır ve uzlaştırma R-01(3) yanlış rezervasyonu "benimseyebilir").

**Savunma.** Veri modelinde CRM yapılandırması tenant başınadır. Ortak CRM senaryosunda `call_id` = `{tenant}:{recording}` olarak gönderilmelidir. Harbor'un kendi CRM'i için okunabilirlik açısından ham `rec_xxx` gönderiliyor (README örneğiyle uyumlu). `acme` CRM'e yazmıyor. Varsayım belgelendi.

### R-08. STT anahtarı çalışma sırasında bozulur

**Saldırı.** 401'i geçici hata gibi ele alan bir sistem, her kayıt için `max_attempts` × geri çekilme kadar bekler. 21 kayıtta bu dakikalarca "işleniyor" görüntüsü demektir. Kalıcı hata gibi ele alan bir sistem ise anahtar düzeltildiğinde her şeyin elle yeniden işlenmesini gerektirir. Daha kötüsü: anahtar bozukken "otomatik olarak faster-whisper'a düş" yaklaşımı değerlendiricinin testini maskeler ve kalite farkını gizler.

**Savunma.** 401/403 kalıcı hatadır. Kayıt hemen `FAILED(transcribe, stt_auth_error)` olur. Anahtar düzeltilince tek komutla (`reprocess --all-failed --step transcribe`) devam edilir. Opsiyonel bir devre kesici (circuit breaker), ilk 401'den sonra diğer kayıtları boşuna denemeden aynı hatayla işaretler. Sessiz yedek sağlayıcıya geçiş **yok**; sağlayıcı açık yapılandırmayla seçilir.

### R-09. LLM tabanlı çıkarım deterministik değil

**Saldırı.** Aynı transkript, yeniden çalıştırmada farklı bir `requested_start` üretebilir. Bu farklı bir slot ve farklı bir anahtar demek; ilk denemenin kayıp cevabıyla birleşirse R-01 tekrarlanır.

**Savunma.** Çıkarım çıktısı kalıcılaştırılır. CRM adımı yalnız bu kalıcı çıktıyı kullanır. `reprocess` başarılı adımları tekrar etmez. Ayrıca sıcaklık 0 ve JSON şema doğrulaması kullanılır. Saat çözümleme (göreli → mutlak) LLM'de değil, test edilebilir **saf Python fonksiyonunda** yapılır: LLM yalnız "perşembe, öğleden sonra" gibi yapılandırılmış alanları çıkarır.

---

## Kaynaklar

- Radford, A. ve ark. (2022). *Robust Speech Recognition via Large-Scale Weak Supervision.* arXiv:2212.04356.
- Koenecke, A. ve ark. (2024). *Careless Whisper: Speech-to-Text Hallucination Harms.* ACM FAccT.
- SYSTRAN/faster-whisper, GitHub README (benchmark bölümü).
- Groq: [Speech-to-Text docs](https://console.groq.com/docs/speech-to-text), [whisper-large-v3 model sayfası](https://console.groq.com/docs/model/whisper-large-v3), [Rate limits](https://console.groq.com/docs/rate-limits.md), [Groq blog: ASR on GroqCloud](https://groq.com/blog/largest-most-capable-asr-model-now-faster-on-groqcloud); üçüncü taraf özet: [developersdigest Groq rate limits](https://www.developersdigest.tech/rate-limits/groq).
- Türkçe benchmark'lar: [BuzzASR/turkish](https://huggingface.co/BuzzASR/turkish), [ElevenLabs Turkish STT](https://elevenlabs.io/speech-to-text/turkish), [selimc/whisper-large-v3-turbo-turkish](https://huggingface.co/selimc/whisper-large-v3-turbo-turkish/blob/main/README.md?code=true).
- PostgreSQL Documentation, *Row Security Policies* (§5.9) ve `CREATE POLICY`, `ALTER TABLE ... FORCE ROW LEVEL SECURITY`, `SELECT ... FOR UPDATE SKIP LOCKED`.
- Microsoft Azure Architecture Center, *Multitenant SaaS database tenancy patterns*; AWS, *Multi-tenant data isolation with PostgreSQL Row Level Security*.
- Kleppmann, M. (2017). *Designing Data-Intensive Applications*, O'Reilly, böl. 7, 8, 11.
- Helland, P. (2007). *Life beyond Distributed Transactions: an Apostate's Opinion.* CIDR.
- Richardson, C. (2018). *Microservices Patterns*, Transactional Outbox.
- Stripe Engineering, *Designing robust and predictable APIs with idempotency* (Idempotency-Key deseni).
- Celery dokümantasyonu: `acks_late`, `task_reject_on_worker_lost`, Redis broker `visibility_timeout` uyarıları. BullMQ dokümantasyonu: stalled jobs. ARQ dokümantasyonu: job timeout / retry.
- Microsoft Presidio dokümantasyonu (custom recognizers, context enhancement). turkish-nlp-suite (spaCy Türkçe modelleri).
- ISO 13616 (IBAN), ISO/IEC 7812-1 (Luhn), T.C. Kimlik No doğrulama algoritması.
