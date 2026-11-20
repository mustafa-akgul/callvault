# Decisions (ADR)

Belgeler Türkçe, README İngilizce.

Her karar bir Architecture Decision Record. **Numaralar sabit:** `ADR-NN` = önceki sürümlerdeki `D-NN`, bu yüzden `RESEARCH_AND_LITERATURE.md` (R-xx riskleri, § kıyaslamalar) içindeki referanslar geçerliliğini koruyor.

Her ADR'de şu başlıklar var: **Status** · **Context** · **Decision** · **Consequences** (+ olumlu / − bedel) · **Rejected alternatives** · **Revisit if**.

Statü değerleri: *Accepted* · *Amended by ADR-x* (kısmen değişti) · *Superseded by ADR-x* (yerine yenisi geldi).

| # | Karar | Status | Aşama |
|---|---|---|---|
| 01 | STT sağlayıcısı: Groq, faster-whisper alternatif | Accepted | 1 |
| 02 | Multi-tenancy: ortak tablolar + FORCE RLS | Accepted | 1 |
| 03 | Kuyruk: PostgreSQL + SKIP LOCKED | Accepted | 1 |
| 04 | Türkçe PII: hibrit maskeleme | Amended by 22 | 1 |
| 05 | Aksiyon çıkarımı: LLM | Superseded by 24 | 1 |
| 06 | CRM'e idempotent gönderim | Amended by 27 | 1 |
| 07 | Slot seçim kuralları | Amended by 26 | 1 |
| 08 | Ses doğrulama adımı | Accepted | 1 |
| 09 | Byte-özdeş kopya kayıtlar | Accepted | 1 |
| 10 | Dil, çatı, paketleme | Accepted | 1 |
| 11 | Fail-closed tenant bağlamı | Accepted | 2 |
| 12 | Roller ve yetkiler | Accepted | 2 |
| 13 | Kira + fencing ile kuyruk | Accepted | 2 |
| 14 | Durum makinesi ve adım adları | Accepted | 2 |
| 15 | SHA-256 ile tekilleştirme | Accepted | 2 |
| 16 | Validasyon eşikleri | Accepted | 2 |
| 17 | `crm_actions` anahtarı ve benzersizlik | Amended by 27 | 2 |
| 18 | STT arayüzü ve hata sınıfları | Accepted | 3 |
| 19 | Stereo kanal → konuşmacı rolü | Accepted | 3 |
| 20 | Ham transkript: şifreli, satıra bağlı | Accepted | 3 |
| 21 | Türkçe sayı normalizasyonu | Accepted | 3 |
| 22 | PII kuralları ve checksum politikası (+ STT e-posta biçimleri, gerçek çalıştırma sonrası) | Accepted, amended | 3, 9 |
| 23 | MASKING hata politikası | Accepted | 3 |
| 24 | Kural tabanlı aksiyon çıkarımı | Accepted | 4 |
| 25 | Türkçe zaman çözümleme | Accepted | 4 |
| 26 | Slot seçimi | Accepted | 4 |
| 27 | Idempotency ve sıfır mükerrer | Accepted | 4 |
| 28 | CRM client'ında yeniden deneme | Accepted | 4 |
| 29 | CRM hata görünürlüğü ve reprocess | Accepted | 4 |
| 30 | API anahtarıyla tenant kimliği ve güven sınırı | Accepted | 5 |
| 31 | Tenant arayüzü: CLI (+ Rich) | Accepted | 5 |
| 32 | `demo-chaos`: ayrı veritabanı, senaryolu STT | Accepted | 5 |
| 33 | Çıktı güvenliği: markup escape ve saat dilimi | Accepted | 5 |
| 34 | `remask`: tamamlanmış kayıtları yeniden maskeleme | Accepted | 10 |
| 35 | Metin temizliği: "yüz yüze", zamir "o", Whisper halüsinasyonları | Accepted | 11 |
| 36 | Son açıklar: isimler, "10 bin", 429'un deneme tüketmemesi | Accepted | 12 |

---

## Aşama 1: analiz ve mimari

### ADR-01. STT sağlayıcısı
- **Status:** Accepted (arayüz ve hata eşlemesi ADR-18'de).
- **Context:** 21 Türkçe telefon kaydı (8 kHz stereo, ≈55 dk) ücretsiz olarak ve temiz bir makinede transkribe edilmeli. Değerlendirici çalışma sırasında STT anahtarını bozacak.
- **Decision:** Varsayılan sağlayıcı Groq `whisper-large-v3`. `faster-whisper` aynı arayüzün arkasında, `STT_PROVIDER=faster_whisper` ile açıkça seçiliyor. Otomatik yedeğe geçiş yok.
- **Consequences:**
  - (+) Aynı model ağırlıkları. Ücretsiz kota veri setine yetiyor (saatte 7.200 ses-sn, dosya ≤ 25 MB). Temiz makinede ~3 GB model indirmek gerekmiyor. "Anahtarı bozma" testi gerçekten uygulanabiliyor.
  - (−) Ham ses (PII içeriyor) üçüncü bir tarafa gidiyor. Değerlendiricinin ücretsiz bir anahtar alması gerekiyor. Kota sınırı yeniden denemelerle zorlanabilir.
- **Rejected alternatives:**
  - Yalnız yerel faster-whisper: bozulacak bir anahtar olmadığı için o test uygulanamazdı; CPU'da 30–60 dk sürer.
  - ElevenLabs: ücretsiz dakikaları az.
  - Deepgram / AssemblyAI: deneme kredisi tükenebilir.
- **Revisit if:** KVKK açısından veri işleme sözleşmesi olmayan bir sağlayıcıya gerçek veri gidecekse (yerel ya da GPU'lu self-host'a geçilir). Telefon sesinde ölçülen kalite yetersizse.

### ADR-02. Multi-tenant veri izolasyonu
- **Status:** Accepted (ayrıntılar ADR-11, 12 ve 30'da).
- **Context:** Bir tenant'ın verisi diğerine asla görünmemeli. İzolasyon "bozması zor olmalı, bozulursa bir test yakalamalı".
- **Decision:** Ortak tablolar, `tenant_id`, `ENABLE` + `FORCE ROW LEVEL SECURITY`, yalnız `SET LOCAL` ile bağlam, `WITH CHECK`, bileşik yabancı anahtarlar `(tenant_id, …)`.
- **Consequences:**
  - (+) Unutulan bir `WHERE` sızıntıya yol açmıyor. Tek bir katalog testi, yeni bir tabloda RLS'in unutulmasını yakalıyor. Tek migrasyon yeterli.
  - (−) RLS görünmez bir mantık katmanı ekliyor. Tablo sahibi rolüyle bağlanmak riskli; rol ayrımı zorunlu (R-05). Tenant başına yedekleme zor.
- **Rejected alternatives:**
  - Yalnız uygulama seviyesinde `WHERE`: tek bir unutulan filtre sızıntı demek.
  - Tenant başına şema: `search_path` sızıntısı riski, N kez migrasyon.
  - Tenant başına veritabanı: işletme yükü ağır.
- **Mühendislik notu (enterprise etkisi):**
  - *Güvenilirlik:* İzolasyon uygulama kodunun doğruluğuna bağlı değil. Yeni bir sorgu, yeni bir geliştirici ya da bir ORM hatası filtreyi unutsa bile veritabanı ya boş sonuç veriyor ya da (bağlam yoksa) hata veriyor. Denetimde "izolasyon nerede?" sorusunun cevabı tek bir yer: migration 002 ve onu doğrulayan katalog testi.
  - *Maliyet:* Tenant sayısı arttıkça şema ve migrasyon sayısı artmıyor. 1 tenant ile 1000 tenant için aynı tek migrasyon koşuyor, aynı bağlantı havuzu kullanılıyor. Tenant başına şema yaklaşımında her deploy N şemaya migrasyon, her yeni tenant bir provisioning adımı demek.
  - *Sınır:* Fiziksel ayrım veya tenant başına yedekleme sözleşmesel bir şartsa RLS bunu karşılamıyor (aşağıdaki "Revisit if").
- **Revisit if:** Bir müşteri sözleşmesel olarak fiziksel ayrım isterse (o tenant için hibrit, tenant başına DB).

### ADR-03. Kuyruk ve durum makinesi
- **Status:** Accepted (gerçekleme ADR-13'te).
- **Context:** SIGKILL'de iş kaybolmamalı, paralel worker'lar aynı kaydı iki kez işlememeli, durum her an görünür olmalı, başarısız adımdan devam edilebilmeli.
- **Decision:** PostgreSQL iş tablosu + `FOR UPDATE SKIP LOCKED` + kira, heartbeat ve fencing.
- **Consequences:**
  - (+) Durum ve iş aynı transaction'da güncelleniyor (çift yazma yok). Ek altyapı yok. "Adımdan devam" doğal hâle geliyor.
  - (−) Kendi yazdığımız kuyruk kodu (R-06); bunu SIGKILL ve paralellik testleriyle telafi ediyoruz. Yoklama (polling) gecikmesi var.
- **Rejected alternatives:**
  - Celery + Redis: varsayılan ayarlarla SIGKILL'de iş kaybolur, `visibility_timeout` çift teslimat yapabilir, durum iki yerde tutulur.
  - ARQ, BullMQ, Redis Streams: hepsinde çift yazma problemi var; BullMQ ayrıca ikinci bir dil getirir.
  - Procrastinate: makul bir alternatif, not edildi.
- **Mühendislik notu (enterprise etkisi):**
  - *Güvenilirlik:* "İş tamamlandı" ile "sonuç kaydedildi" aynı commit. Celery + Redis'te bu iki ayrı sistemde iki ayrı yazma; aralarında bir çökme ya kayıp iş ya da hayalet tamamlanma demek. Burada bu pencere yok, gerçek SIGKILL testiyle doğrulandı.
  - *Maliyet ve işletme:* Çalıştırılacak, izlenecek, yedeklenecek, yamalanacak ikinci bir durum deposu (Redis broker) yok. Kuyruğun durumu veritabanı yedeğiyle birlikte yedekleniyor; felaket sonrası geri dönüşte kuyruk ile kayıtlar birbirini tutuyor. Gözlem için yeni bir araç gerekmiyor, kuyruk SQL ile sorgulanıyor (`metrics`).
  - *Sınır:* Bu bir veritabanı yoklama kuyruğu. Saniyede binlerce işte ya da çok servisli fan-out'ta özel bir broker daha uygun (aşağıdaki "Revisit if").
- **Revisit if:** Saniyede binlerce iş olursa ya da birden fazla servis aynı kuyruğu tüketmeye başlarsa.

### ADR-04. Türkçe PII maskeleme yaklaşımı
- **Status:** Amended by ADR-22 (etiketler kullanıcının istediği standart metinlere çevrildi; NER yerine bağlam kalıpları ve ad listeleri kullanıldı).
- **Context:** Ad, telefon, e-posta, TCKN, kart ve IBAN maskelenmeli. Girdi STT çıktısı: sayılar bölünmüş ya da kelimeyle yazılmış, kesme işaretleri eksik olabilir.
- **Decision:** Hibrit yaklaşım: sayı normalizasyonu + doğrulayıcılı regex + isimler için bağlam. Ham PII LLM'e gönderilmiyor.
- **Consequences:**
  - (+) Yapılandırılmış PII'de deterministik ve test edilebilir.
  - (−) Ad recall'ı sınırlı.
- **Rejected alternatives:**
  - Yalnız regex: adları yakalamaz.
  - Presidio: hazır Türkçe modeli yok.
  - LLM: ham PII üçüncü tarafa gider, deterministik değil.
- **Revisit if:** Gerçek veride ad recall'ı düşük çıkarsa (yerel bir Türkçe NER modeli eklenir).

### ADR-05. Aksiyon çıkarımı için LLM
- **Status:** Superseded by ADR-24.
- **Context / Decision (asıl hâli):** Maskeli metinden JSON şemalı LLM çıktısı üretilecek, tarih hesabı Python'da yapılacaktı.
- **Neden değişti:** Bu oturumda bir LLM anahtarı yoktu. Ayrıca CRM gövdesinin her denemede aynı kalması için deterministik bir çıkarım gerekiyordu (R-09).

### ADR-06. CRM'e idempotent ve chaos'a dayanıklı gönderim (tasarım)
- **Status:** Amended by ADR-27. Anahtar formatı kullanıcının belirlediği biçime çevrildi: `<tenant>:<action>:<recording>:<slot>`.
- **Context:** `CHAOS=1` altında 429, %15 oranında 500, %10 oranında 8 sn gecikme ve %5 oranında kayıp cevap var.
- **Decision:** Deterministik anahtar, gönderimden önce kalıcılaştırma, uzlaştırma, seri rezervasyon ve global hız sınırına uyum.
- **Rejected alternatives:** Her denemede rastgele anahtar (kayıp cevapta mükerrer üretir). Kayıt başına sabit anahtar (slot değişince 422 alınır).

### ADR-07. Slot seçim kuralları (tasarım)
- **Status:** Amended by ADR-26 (kesin kurallar ve testler orada).
- **Decision:** Talep bir kısıt kümesine çevriliyor. "İstenen zamandan sonraki en erken slot" aynı günle sınırlı değil. Hiçbir şey uymazsa `needs_followup`.

### ADR-08. Ses doğrulama adımı
- **Status:** Accepted (eşikler ADR-16'da).
- **Context:** kesik örnek kayıt kesik bir dosya: başlık 158 sn diyor, dosya 2 KB ve sessiz. Whisper sessizlikte halüsinasyon üretebilir.
- **Decision:** STT'den önce bir `VALIDATION` adımı var. Bozuk dosya **kalıcı** hata sayılıyor ve görünür biçimde FAILED oluyor.
- **Consequences:**
  - (+) Sahte bir transkript ve sahte bir CRM aksiyonu oluşmuyor.
  - (−) Tamamen sessiz geçen gerçek bir çağrı da reddedilir.
- **Rejected alternatives:** Doğrudan STT'ye göndermek. Sessizce atlamak ("silently skipped" yasağı).

### ADR-09. Byte-özdeş kopya kayıtlar
- **Status:** Accepted (gerçekleme ADR-15'te).
- **Context:** kopya örnek kayıt, ilk kayıt'in byte-düzeyinde kopyası.
- **Decision:** Kopya tespit ediliyor: `DUPLICATE` olarak işaretleniyor, `duplicate_of` dolduruluyor ve kayıt CRM'e girmiyor.
- **Consequences:**
  - (+) Aynı kişi için ikinci bir toplantı veya şikâyet açılmıyor.
  - (−) Değerlendirici iki kaydı bağımsız bekliyorsa sonuç farklı görünür; bu yüzden durum ve gerekçe görünür tutuluyor.
- **Rejected alternatives:** Bağımsız işlemek (mükerrer oluşur). Atlamak (sessiz kayıp sayılır).
- **Revisit if:** Harbor "her `recording_id` ayrı bir çağrıdır" derse.

### ADR-10. Dil, çatı ve paketleme
- **Status:** Accepted.
- **Decision:** Python 3.12, psycopg 3 (ham SQL, ORM yok), httpx, cryptography, rich, pytest, Docker Compose (`db`, `crm`, `migrate`, `ingest`, `worker`; `tests` ve `demo` profilleri).
- **Consequences:**
  - (+) RLS ve kira mantığı SQL'de açıkça görünüyor. CRM mock da Python.
  - (−) ORM'in sağladığı tip güvenliği ve migrasyon araçları yok; migrasyonlar sıralı SQL dosyaları.
- **Rejected alternatives:** FastAPI + SQLAlchemy. Bu kapsam için gereğinden ağırdı ve RLS davranışını gizlerdi.

## Aşama 2: şema, RLS, kuyruk

### ADR-11. Fail-closed tenant bağlamı
- **Status:** Accepted.
- **Context:** Tenant bağlamı olmadan yapılan sorgu boş sonuç değil, SQL hatası vermeli.
- **Decision:** Politikalar `app_current_tenant()` fonksiyonunu kullanıyor. Fonksiyon, bağlam NULL **veya boş string** ise `insufficient_privilege` hatası fırlatıyor.
- **Consequences:**
  - (+) Önemli bir tuzağı kapatıyor: `SET LOCAL` ile ayarlanan değer, transaction bitince oturumda boş string olarak kalıyor. Bağlantı yeniden kullanıldığında düz bir `current_setting()` hata vermez, sessizce boş sonuç döner. Bunu test ediyoruz; fonksiyon bozulduğunda 8 test kırılıyor (mutasyon testi).
  - (−) Satır başına bir plpgsql çağrısı. Taranacak satır olmayan (boş) tabloda hata çıkmıyor; sızacak veri de yok.
- **Rejected alternatives:** Doğrudan `current_setting()` (yukarıdaki tuzak). `missing_ok` ile NULL (fail-open).

### ADR-12. Roller ve yetkiler
- **Status:** Accepted (genişletildi: ADR-20 sütun bazlı yetki, ADR-30 `api_keys`).
- **Decision:**
  - `app_owner`: şema sahibi, migrasyonları çalıştırıyor.
  - `app_worker`: SELECT, INSERT, UPDATE.
  - `app_user`: yalnız SELECT; `jobs` tablosuna ve ham transkript sütununa erişimi yok.
  - Hiçbiri SUPERUSER ya da BYPASSRLS değil; **hiçbirinde DELETE yok**. Compose'daki geliştirme parolaları `.env` ile ezilebiliyor.
- **Consequences:**
  - (+) Uygulama şemanın sahibi değil, yani politikayı değiştiremiyor (pen-test'te doğrulandı).
  - (−) Compose'daki varsayılan geliştirme parolaları, "tek komutla çalışır" gereksinimi için bilinçli bir taviz.
- **Rejected alternatives:** Tek bir uygulama rolü (sahip olacağı için RLS'i kapatabilirdi).
- **Mühendislik notu (enterprise etkisi):** Uygulamaya ait bir SQL injection açığı bile olsa saldırgan yalnız o rolün yetkileriyle sınırlı kalıyor. Politikayı kapatamıyor, rol değiştiremiyor, veri silemiyor; okuma rolü ham transkripti göremiyor. Bu, en az yetki ilkesinin veritabanında uygulanması ve 43 pen-test'in büyük kısmı bunu doğruluyor. Ek maliyeti sıfır: üç rol tek bir migrasyonda tanımlanıyor.

### ADR-13. Kuyruk gerçeklemesi: kira, heartbeat, fencing
- **Status:** Accepted.
- **Decision:**
  - Kayıt başına bir `jobs` satırı.
  - Claim `SKIP LOCKED` ile yapılıyor; kira `locked_until`, fencing `lease_token` ile.
  - `attempts` claim anında artırılıyor. Worker'ı her seferinde öldüren bir iş `FAILED(worker_lost)` olarak kalıyor.
  - `jobs` tablosuna yalnız `app_worker` `USING (true)` politikasıyla erişiyor. Bu tasarımdaki tek bilinçli tenant'lar arası yüzey; tabloda PII yok.
  - Her geçiş `recording_events` tablosuna yazılıyor.
  - Başlangıç parametreleri: kira 60 sn, heartbeat 15 sn, `max_attempts` 5, geri çekilme 2^n sn + jitter (üst sınır 60 sn).
- **Consequences:**
  - (+) Zombi bir worker'ın geç yazması reddediliyor. SIGKILL edilen bir worker'ın işi kira dolunca başka bir worker'a geçiyor (test edildi; gerçek süreç öldürüldü).
  - (−) Heartbeat koparsa iş iki kez *çalışabilir*, ama iki kez *yazılamaz*. Dış yan etkiler bu yüzden yine idempotent olmalı (ADR-27).
- **Rejected alternatives:** Adım süresince transaction'ı açık tutmak (STT dakikalar sürüyor; bağlantı havuzu tükenir).
- **Mühendislik notu (enterprise etkisi):** Yatay ölçekleme koordinasyon servisi gerektirmiyor: `--scale worker=N` yeterli, worker'lar birbirini bilmiyor. Fencing token, ağ bölünmesi ya da GC duraklaması gibi "yavaş ama ölmemiş" worker durumlarında bile eski kiranın yazmasını reddediyor; bu, dağıtık kilitlerde en sık atlanan hata sınıfı.

### ADR-14. Durum makinesi ve adım adları
- **Status:** Accepted (ADR-29 ile ayrıntılı CRM adımları eklendi).
- **Decision:**
  - Durumlar: `PENDING → VALIDATING → TRANSCRIBING → MASKING → PROCESSING_CRM → COMPLETED`, ayrıca `FAILED` ve `DUPLICATE`.
  - Adımlar ayrı bir enum'da. Tutarlılık CHECK kısıtlarıyla zorlanıyor (`FAILED ⇔ failed_step`, `DUPLICATE ⇔ duplicate_of`).
  - Aksiyon çıkarımı `PROCESSING_CRM` adımının içinde yapılıyor; ayrı bir EXTRACTING durumu yok.
- **Consequences:**
  - (+) Durum ve açıklaması hiçbir zaman çelişemiyor.
  - (−) Çıkarım ayrı bir durum olarak görünmüyor; ancak çıktısı `transcripts.extraction` sütununda saklanıyor.

### ADR-15. SHA-256 ile tekilleştirme
- **Status:** Accepted.
- **Decision:**
  - Hash ingest sırasında hesaplanıyor; karar VALIDATION adımında veriliyor.
  - Aynı tenant içinde, `(recorded_at, id)` sırasında önce gelen kayıt kanonik kabul ediliyor.
  - Bir trigger, `DUPLICATE` bir kayda CRM aksiyonu eklenmesini reddediyor.
- **Consequences:**
  - (+) Sonuç hangi worker'ın önce işlediğinden bağımsız. Aynı ses farklı tenant'larda mükerrer sayılmıyor.
  - (−) Kanonik kayıt sonradan başarısız olursa kopyası `DUPLICATE` kalır. Kanonik kayıt `reprocess` edilince akış düzelir.
- **Rejected alternatives:** MD5 (istenen buydu). Çakışma üretilebildiği için kasıtlı bir "mükerrer gösterme" saldırısına açık; maliyet farkı önemsiz.

### ADR-16. Validasyon eşikleri
- **Status:** Accepted.
- **Decision:** RIFF/PCM 16-bit; beyan edilen veri boyutu gerçek boyuttan büyükse `truncated_audio`; 1 sn'den kısa ise `too_short`; RMS < 50 ise `silent_audio`; dosya yoksa `file_missing`; yol ses dizininin dışına çıkıyorsa `bad_path`.
- **Consequences:** (+) Eşikler ölçümle seçildi: gerçek kayıtlarda RMS 1.694–2.000, kesik örnek kayıt'de 9,2.

### ADR-17. `crm_actions` anahtarı ve benzersizlik
- **Status:** Amended by ADR-27 (rezervasyonda HTTP anahtarı slotu da içeriyor).
- **Decision:**
  - `idempotency_key` generated column: `<tenant>:<action>:<recording>`.
  - `UNIQUE (tenant_id, recording_id, action_type)`.
- **İstenenden sapma:** İstek `(recording_id, action_type)` idi. Kayıt kimliği bileşik olduğu için tenant'sız bir kısıt, iki tenant'ın aynı id'li kayıtlarını birbirine bağlardı.

## Aşama 3: STT ve PII

### ADR-18. STT arayüzü ve hata sınıfları
- **Status:** Accepted.
- **Decision:**
  - Hata eşlemesi: 401/403 → kalıcı `stt_auth_error`; 400/413/415 → kalıcı `stt_rejected`; 429 → geçici, `Retry-After` dikkate alınıyor; 5xx, zaman aşımı, ağ hatası ve bozuk cevap → geçici; anahtar yoksa `stt_not_configured` ve API hiç çağrılmıyor.
  - Hata mesajları sağlayıcı, model ve kanal bilgisini içeriyor; API anahtarı `***` ile gizleniyor.
- **Consequences:**
  - (+) Bozuk anahtar dakikalarca yeniden deneme yerine anında FAILED oluyor; `reprocess` TRANSCRIPTION adımından devam ediyor.
  - (−) Bir kanal hata verirse iki kanal da yeniden transkribe ediliyor.
- **Rejected alternatives:** 401'i geçici saymak (boşuna bekler). Otomatik yerel yedek (hatayı gizler).

### ADR-19. Stereo kanal → konuşmacı rolü
- **Status:** Accepted.
- **Decision:** Ses kanal başına mono WAV'a bölünüyor. Temsilci kanalı hizmet ifadeleri ("Harbor", "nasıl yardımcı olabilirim", …) ile; sonuç çıkmazsa ilk konuşan kanal olarak seçiliyor. Atama ve yöntemi `stt_meta`'ya yazılıyor.
- **Consequences:**
  - (+) Diarizasyon modeli gerekmiyor (kanalların ayrık olduğu ölçüldü).
  - (−) 2 kat STT çağrısı. Sezgisel; gerçek kayıtlarla doğrulanmadı.

### ADR-20. Ham transkript: şifreli, satıra bağlı, okuma rolüne kapalı
- **Status:** Accepted.
- **Decision:**
  - AES-256-GCM; associated data `"<tenant>/<recording>"`.
  - `app_user` sütun bazlı GRANT nedeniyle `raw_text_enc` sütununu okuyamıyor.
  - Anahtar `TRANSCRIPT_KEY` ortam değişkeninden ya da dosyadan alınıyor; dosyayı `migrate`, Docker `keys` volume'unda oluşturuyor.
- **Consequences:**
  - (+) Maskeleme STT'yi tekrar çağırmadan yeniden çalıştırılabiliyor. Bir tenant'ın şifreli verisi başka bir satırda çözülmüyor (pen-test'te doğrulandı).
  - (−) Anahtar rotasyonu yok (`v1` sürüm baytı buna hazırlık). Anahtar kaybolursa ham metin de kaybolur, maskeli metin kalır.
- **Rejected alternatives:** Düz metin `raw_text` (istenen isim buydu). pgcrypto (anahtar SQL parametresi olarak loglara düşebilir).

### ADR-21. Türkçe sayı normalizasyonu
- **Status:** Accepted.
- **Decision:** Dikte edilen gruplar korunuyor ("sıfır beş yüz otuz iki …" → `0 532 …`), son kelimedeki ek korunuyor (`15'te`), tek başına "bir" çevrilmiyor.
- **Consequences:**
  - (+) Telefon/IBAN regex'leri ve zaman ayrıştırması basitleşiyor.
  - (−) Maskeli metin normalleştirilmiş sayılar içeriyor ("2 kez").

### ADR-22. PII kuralları ve checksum politikası
- **Status:** Accepted.
- **Decision:**
  - E-posta (yazılı ve sesli), IBAN (mod-97; "TR" söylenmemişse 24 hane), kart (16 hane, Luhn), TCKN (resmî algoritma), telefon (önek kuralları) ve isim (bağlam kalıpları + ad/soyad listeleri + belirsiz adlar için cümle başı kuralı).
  - Etiketler standart: `[İSİM MASKELENDİ]`, `[TELEFON MASKELENDİ]`, …
  - Checksum tutmuyorsa maskelenmiyor. **İstisna:** hemen önce açık bir bağlam kelimesi ("TC kimlik numaram", "IBAN", "kart numaram") varsa ve uzunluk doğruysa yine maskeleniyor.
- **Consequences:**
  - (+) STT tek bir haneyi yanlış duyarsa gerçek kimlik numarası açıkta kalmıyor.
  - (−) Listede olmayan ve bağlama uymayan isimler kaçabilir.
- **Rejected alternatives:** Saf checksum politikası (istenen buydu; STT hatasında sızdırır). Ağır NER modeli (~500 MB + torch).
- **Amendment (2026-11-08, gerçek Groq çalıştırmasından sonra): STT biçimli e-posta adresleri.**
  - *Bulgu:* whisper-large-v3 sesli e-postayı `@` olmadan yazıyor: noktalar `.` olarak, "at/et" kelime olarak ya da noktaların arasına gömülü. Eski kural yalnız "nokta com" yazımını tanıyordu ve 19 gerçek transkriptte **0** e-posta maskeledi. en uzun kayıt'daki adreslerden biri şikâyet özetinde CRM'e gitti.
  - *Karar:* Dört yeni kural eklendi. (1) `ad.at.alan.ai` biçimi. (2) `[ad] at alan.com`. (3) Bilinen bir posta sağlayıcısının önündeki her şey (`ad579.gmail.com`, `… email.com`). (4) Alan adı biçimli bir kelime: sonekten önce en az iki ad etiketi varsa (`ad.soyad.com`) ya da aynı konuşma sırasında hemen önce "mail / e-posta" geçiyorsa maskeleniyor.
  - *Bilinçli sınır:* Tek başına geçen `harbor.ai`, `www.….com.tr`, `….gov.tr` gibi alan adları maskelenmiyor; bunlar adres değil, web sitesi. Bu durumlar test edildi.
  - *Sonuç:* Gerçek 19 transkriptte 0 → 14 e-posta maskelendi, kalan e-posta kalıbı 0, yanlış pozitif görülmedi (`remask` doğrulaması, çıktı yalnız `x`/`9` şeklinde). 16 yeni test eklendi.
  - *Kalan risk:* Kural tabanlı yaklaşım, STT'nin ileride üreteceği yeni biçimleri kaçırabilir. Bu yüzden gerçek veride düzenli örneklem kontrolü gerekiyor.

### ADR-23. MASKING hata politikası
- **Status:** Accepted.
- **Decision:** Maskeleme deterministik olduğu için maskeleyicideki bir istisna kalıcı hata sayılıyor (`masking_error`). `decrypt_failed`, `transcript_missing` ve `empty_transcript` de kalıcı. `encryption_key_missing` STT çağrısından **önce** veriliyor.
- **Consequences:** (+) Aynı girdide tekrar oluşacak bir hata için boşuna yeniden denenmiyor. `reprocess` MASKING adımından başlıyor ve STT'yi çağırmıyor.

## Aşama 4: CRM

### ADR-24. Kural tabanlı aksiyon çıkarımı
- **Status:** Accepted (ADR-05'in yerine geçti).
- **Decision:**
  - Toplantı: toplantı kelimesi geçen ilk turdan itibaren konuşma inceleniyor. Zaman önce müşteriden çözülüyor; orada yoksa ("evet, olur") temsilcinin önerdiği zaman kullanılıyor.
  - Şikâyet: müşteri cümlelerinden, olumsuzlamalar ("sorun değil") hariç tutularak seçiliyor; özet en fazla 240 karakter.
  - Çıkarım **bir kez** yapılıyor ve `transcripts.extraction` + `crm_actions` tablolarına yazılıyor.
- **Consequences:**
  - (+) Deterministik, maliyetsiz, PII dışarı çıkmıyor; yeniden deneme isteği değiştiremiyor.
  - (−) Dolaylı ifadeler kaçabilir; gerçek transkriptlerle ölçülmedi.
- **Revisit if:** Gerçek veride hata oranı yüksekse (maskeli metin üzerinde JSON şemalı LLM).

### ADR-25. Türkçe zaman çözümleme
- **Status:** Accepted.
- **Decision:**
  - Referans `recorded_at`; saat dilimi sabit UTC+3 (Türkiye yaz saati uygulamıyor).
  - Gün ve saat için **son söylenen** geçerli ("salı olmaz, perşembe olur").
  - "Saat 3" mesai saati varsayımıyla 15:00. Gün bölümleri: sabah 9–12, öğle 12–14, öğleden sonra 13–18, akşamüstü 16–18.
  - Tek bir gün + saat → kesin talep. Saat var ama gün yoksa → o saat herhangi bir günde (esnek).
- **Consequences:** (−) "Saat 3 = 15:00" bir varsayım; sabah 3 için yanlış olur, ama mesai dışında zaten slot yok.

### ADR-26. Slot seçimi
- **Status:** Accepted (ADR-07'yi kesinleştirdi).
- **Decision:**
  1. Kesin saat ve slot boş → o slot (`exact`).
  2. Slot dolu ya da takvimde böyle bir slot yok → istenen saatten sonraki ilk boş slot (`next_after_requested`).
  3. Esnek istek → kısıtlara uyan ilk boş slot (`first_matching`). Sabit bir gün söylendiyse o güne bağlı kalınıyor; hafta günü söylendiyse bir sonraki haftaya geçilebiliyor.
  4. Hiçbiri yoksa → `NEEDS_FOLLOWUP`, gerekçesiyle; CRM'e yazma gitmiyor.
  - Görüşmeden önce başlayan slot hiç seçilmiyor; 409 almış slotlar dışlanıyor. İstenen, rezerve edilen ve gerekçe saklanıyor.
- **Consequences:**
  - (+) Saf bir fonksiyon; gerçek takvimle 13 test.
  - (−) Aynı slotu isteyen iki çağrıdan hangisinin kazanacağı `recorded_at` sırasına göre değil, zamanlamaya bağlı (demo çıktısında görülüyor).

### ADR-27. Idempotency ve sıfır mükerrer
- **Status:** Accepted.
- **Decision:**
  1. Anahtarlar generated column: rezervasyonda `<tenant>:booking:<recording>:<slot_start>`, şikâyette `<tenant>:complaint:<recording>`.
  2. Deneme satırı (slot, anahtar, gövde) POST'tan **önce** commit ediliyor; gövde hiçbir zaman yeniden oluşturulmuyor.
  3. Sonuç belirsizse aynı anahtar ve aynı gövde tekrar gönderiliyor. Yeni slot **yalnız kesin bir 409'dan** sonra seçiliyor.
  4. Her denemeden önce `GET /bookings` ile uzlaştırma yapılıyor (`call_id` + nottaki `tenant=` etiketi).
  5. Veritabanı garantileri: tek `PLANNED` deneme, kayıt başına tek başarı, `UNIQUE (tenant, recording, action)`, mükerrer kayıtları reddeden trigger.
  6. Rezervasyonlar tenant başına bir advisory lock altında yapılıyor.
- **Consequences:**
  - (+) Mükerrer kayıt oluşmuyor. Doğrulama: sahte CRM'de kayıp cevap, CRM yazdıktan sonra çökme, 409 yarışı, CRM'in çökmesi + `reprocess`, veritabanı silinip yeniden işleme; gerçek mock'ta `CHAOS=1` ile 3 seed ve demo.
  - (−) Rezervasyon adımında paralellik yok (CRM zaten 2 istek/sn ile sınırlı).
- **Rejected alternatives:** 409 dışında bir durumda yeni slot denemek (R-01: kendi rezervasyonunu dolu görüp ikinci rezervasyon açar). Uzlaştırmayı yalnız `call_id` ile yapmak (başka tenant'ın kaydını benimseyebilir).
- **Mühendislik notu (enterprise etkisi):** Mükerrer bir toplantı ya da şikâyet kaydı müşteri tarafında görünen bir hata: aynı müşteriye iki davet, iki destek bileti. Garanti tek bir katmana bırakılmadı: anahtar CRM'de, uzlaştırma `GET /bookings` ile, benzersizlik ve trigger veritabanında. Herhangi biri tek başına bozulsa bile diğerleri mükerreri engelliyor. Yeniden işleme (`reprocess`) bu yüzden güvenli bir operasyon; operatörün "tekrar çalıştırırsam ne olur?" diye düşünmesi gerekmiyor.

### ADR-28. CRM client'ında yeniden deneme
- **Status:** Accepted.
- **Decision:**
  - Her çağrıda en fazla 6 deneme; full jitter'lı üstel geri çekilme; 429'da `Retry-After` alt sınır.
  - 500/502/503/504, zaman aşımı ve bağlantı hataları geçici; 400/404/422 kalıcı; 409 hata değil, çağırana dönen bir cevap.
  - Okuma zaman aşımı 20 sn; süreç içi throttle en az 0,55 sn aralık.
- **Consequences:**
  - (+) Kısa kesintiler client içinde atlatılıyor; uzun kesintide iş kuyruğa geri dönüyor ve worker bloke olmuyor.
  - (−) Süreçler arası ortak bir hız sınırlayıcı yok; 429 cevapları zararsız ama süreyi uzatıyor.

### ADR-29. CRM hata görünürlüğü ve reprocess
- **Status:** Accepted.
- **Decision:** `failed_step` = `CRM_BOOKING` veya `CRM_COMPLAINT`; job'un adımı `CRM` olarak kalıyor. `reprocess` `PROCESSING_CRM` adımından devam ediyor. Bir aksiyonun başarısızlığı diğerini engellemiyor; tamamlanmış aksiyonlar atlanıyor.
- **Consequences:** (+) Hatanın nerede olduğu kesin görünüyor; STT ve maskeleme tekrarlanmıyor (test edildi).

## Aşama 5: arayüz, güvenlik, demo

### ADR-30. API anahtarıyla tenant kimliği ve güven sınırı
- **Status:** Accepted.
- **Context:** Önceki CLI'da tenant `--tenant` argümanıyla seçiliyordu; bu, kolayca taklit edilebilen bir header gibiydi.
- **Decision:**
  - Tenant komutları yalnız API anahtarıyla çalışıyor (`CALLVAULT_API_KEY` veya `--api-key`). Tenant anahtardan türetiliyor.
  - `--tenant` verilirse anahtarla karşılaştırılıyor ve uyuşmazsa istek reddediliyor.
  - Anahtarlar yalnız SHA-256 hash olarak saklanıyor (`api_keys`, FORCE RLS). Lookup politikası yalnız sunulan hash'e ait satırı gösteriyor, yani anahtarlar listelenemiyor. Anahtar yalnız kendi tenant bağlamında oluşturulabiliyor. İptal (`revoked_at`) destekleniyor.
- **Güven sınırı:** Tenant'lar yalnız API anahtarı alıyor. Veritabanı parolaları sunucu tarafında kalan sırlar. `app_user` parolasına sahip biri RLS bağlamını seçebilir (oturum değişkeni). Bu risk pen-test'te açıkça belgelendi: o durumda bile erişim yalnız okuma, yalnız maskeli metin; ham transkript, kuyruk ve DELETE yok.
- **Consequences:**
  - (+) Header spoofing, sahte anahtar, iptal edilmiş anahtar, enjeksiyon ve rol yükseltme denemeleri test ediliyor (`tests/test_pen_test.py`, 43 test).
  - (−) Tenant başına ayrı veritabanı rolleri yok. En güçlü izolasyon için bir sonraki adım bu olurdu.
- **Rejected alternatives:** `--tenant` argümanına güvenmek. Anahtarı düz metin saklamak. `SECURITY DEFINER` bir lookup fonksiyonu (RLS'i atlayan fonksiyonları bir testle yasaklıyoruz).

### ADR-31. Tenant arayüzü: CLI (+ Rich)
- **Status:** Accepted.
- **Decision:** `list-recordings` (`--events` ile geçmiş), `get-transcript` (yalnız maskeli metin), `list-actions` (istenen ve rezerve edilen), `metrics`, `reprocess --recording-id | --all-failed` (devam edilen adımı ve atlanan adımları yazıyor), `issue-key`. Okuma tarafı `app/tenant_api.py` içinde ve `app_user` rolüyle çalışıyor.
- **Consequences:**
  - (+) README "API veya CLI" diyor; CLI, değerlendiricinin `docker compose run` ile hemen kullanabileceği en kısa yol.
  - (−) HTTP API yok. `tenant_api` ve `tenant_auth` modülleri bir HTTP katmanının (örn. FastAPI) arkasına doğrudan konabilecek şekilde ayrıldı.
- **Rejected alternatives:** FastAPI servisi. Ek bir servis, port ve kimlik doğrulama yüzeyi getirirdi; README bunu istemiyor.

### ADR-32. `demo-chaos`: ayrı veritabanı ve senaryolu STT
- **Status:** Accepted.
- **Decision:**
  - Demo kendi veritabanında (`<db>_demo`) ve kendi `CHAOS=1` CRM mock'unda çalışıyor.
  - 9 sentetik çağrı (toplantı, şikâyet, kopya, kesik dosya), 3 worker süreci, TRANSCRIPTION ortasında bir worker'ı SIGKILL etme, yerine yenisini başlatma.
  - Sonuç CRM'in kendi state dosyasıyla doğrulanıyor: 0 kayıp, 0 mükerrer.
  - STT senaryolu (`DemoSTT`); geri kalan her şey üretim kod yolu.
- **Neden ayrı veritabanı:** İlk sürüm aynı veritabanını kullanıyordu. Docker'da `docker compose up` ile çalışan üretim worker'ı demo işlerini kaptı (kuyruk tenant'tan bağımsız) ve ses dosyası onun dizininde olmadığı için işler `file_missing` ile düştü. Bu hata yalnız Docker'da görüldü ve düzeltildi.
- **Consequences:**
  - (+) Demo gerçek veriye, gerçek slotlara ve çalışan worker'lara hiç dokunmuyor; anahtar gerektirmiyor; tekrarlanabilir.
  - (−) STT kısmı gerçek değil.

### ADR-33. Çıktı güvenliği: Rich markup escape ve saat dilimi
- **Status:** Accepted.
- **Decision:** Tablolardaki tüm veri hücreleri `rich.markup.escape` ile basılıyor. Saatler, veritabanı oturumunun saat diliminden bağımsız olarak İstanbul saatinde (+03:00) gösteriliyor.
- **Context (Docker'da bulunan iki hata):**
  - Rich, `[groq/whisper-large-v3, channel 1/2]` gibi bir hata metnini markup etiketi sanıp **siliyordu**. Aynı şey `[İSİM MASKELENDİ]` etiketlerinin de görünmemesine yol açardı.
  - Container'daki Postgres oturumu UTC'de çalıştığı için müşterinin istediği 15:00, ekranda 12:00+00:00 olarak görünüyordu.
  - İkisi için de regresyon testi var.

### ADR-34. `remask`: tamamlanmış kayıtları yeni maskeleme kurallarıyla yeniden maskeleme
- **Status:** Accepted (2026-11-08, ADR-22 amendment'ının devamı).
- **Context:** Maskeleme kuralları, kayıtlar COMPLETED olduktan sonra da gelişebiliyor. Gerçek Groq çalıştırmasında sızan e-postalar düzeltildi, ama veritabanındaki maskeli metin eski kurallarla üretilmişti. Sızan metin ayrıca `transcripts.extraction` alanına ve `crm_actions` gövdelerine kopyalanmıştı. `reprocess` yalnız FAILED kayıtları kuyruğa alıyor.
- **Decision:**
  - Ayrı bir operatör komutu: `remask [--tenant] [--recording-id]`. Kuyruktan geçmiyor ve CRM'i çağırmıyor.
  - Her COMPLETED kayıt için tek transaction açılıyor ve kayıt satırı kilitleniyor (çalışan bir worker ile yarışmıyor). Şifreli ham metin çözülüyor, `mask_transcript` aynen yeniden çalışıyor ve `masked_text`, `segments`, `pii_summary` güncelleniyor.
  - Serbest metin kopyaları da temizleniyor: `extraction` alanı, `crm_actions.requested_text` ve payload'lardaki `summary`/`text` anahtarları. id'ler, slotlar ve saatler değişmiyor.
  - Yalnız **son durumdaki** aksiyonlar (SUCCEEDED, NEEDS_FOLLOWUP) yeniden yazılıyor. PENDING, IN_FLIGHT ve FAILED aksiyonların gövdesi aynı Idempotency-Key ile byte-byte yeniden gönderilecek; gövde değişirse CRM 422 döner. Bu yüzden bunlara dokunulmuyor ve raporlanıyor.
  - Değişen her kayıt için `recording_events`'e bir `remasked` olayı yazılıyor. Olayda yalnız sayılar var, metin yok. İkinci çalıştırma hiçbir şeyi değiştirmiyor.
  - Daha önce CRM'e gönderilmiş kayıtların CRM id'leri listeleniyor; CRM tarafını bir insan düzeltmeli.
- **Consequences:**
  - (+) Kural düzeltmesi geçmiş veriye de uygulanabiliyor ve denetlenebiliyor. Gerçek veride 5 kayıt değişti, 14 e-posta maskelendi, veritabanında kalıntı 0, ikinci çalıştırmada 0 değişiklik.
  - (−) CRM'e giden kopya geri alınamıyor (mock'ta güncelleme ucu yok). Bu yüzden asıl savunma, PII'nin CRM'e gönderilmeden önce yakalanması.
- **Rejected alternatives:**
  - `reprocess`'i COMPLETED kayıtlar için MASKING'den başlatmak: CRM adımı tamamlanmış aksiyonları zaten atlıyor, yani kopyalar temizlenmezdi. Ayrıca kaydın durumu gereksiz yere tekrar akardı.
  - Tek seferlik bir SQL yaması: tekrarlanamaz, test edilemez ve denetim kaydı bırakmaz.

### ADR-35. Gerçek STT çıktısı için metin temizliği: deyimler, zamir "o" ve Whisper halüsinasyonları
- **Status:** Accepted (2026-11-08, gerçek Groq çalıştırmasının bulguları).
- **Context:** Gerçek veride üç metin bozulması görüldü. Hiçbiri veri sızdırmıyordu, ama maskeli metni okunmaz hâle getiriyor ve CRM'e giden özetlere taşınıyordu.
  1. "yüz" kelimesi aynı zamanda "surat" demek: "yüz yüze" → "100 100'e", "bu yüzden" → "bu 100'den".
  2. "o" zamirinin hâlleri "on" + ek gibi görünüyor: "onu" → "10'u", "ona" → "10'a", "onun" → "10'un" (gerçek veride 3 örnek).
  3. Whisper, altyazılı videolarla eğitildiği için sessizlikte altyazı jeneriği uyduruyor: "abone ol" (8 kayıtta), "Altyazı M.K." (bozulmuş hâli "Altyaz M"), "İzlediğiniz için teşekkürler".
- **Decision:**
  - `numbers_tr`: "yüz yüze" ve "(bu|o|şu|bunun|onun|ne) yüzden" deyimlerinin kelimeleri sayı sayılmıyor. Tek başına geçen "on" + {u, a, un, da, dan, la} zamir olarak okunuyor; önceki kelime "saat" ise sayı kalıyor ("saat onda" → 10'da). Daha uzun bir sayının parçasıysa da sayı ("yüz ona" → 110'a).
  - `stt.drop_hallucinations`, MASKING adımında `to_turns`'ten önce çalışıyor. Yalnız halüsinasyon ifadelerinden oluşan bir segment tamamen atılıyor. Gerçek konuşmanın içinden yalnız altyazı jeneriği ("Altyazı M.K.") kesiliyor, çünkü bunu hiçbir arayan söylemez. "abone olmak istiyorum" gibi gerçek bir talep korunuyor.
  - **Şifreli ham transkript değişmiyor.** Filtre yalnız türetilmiş metne uygulanıyor; STT'nin ne döndürdüğü delil olarak kalıyor ve kural ileride değişirse `remask` ile yeniden üretilebiliyor.
- **Consequences:**
  - (+) Gerçek veride `remask` sonrası "100 100'e/100'den", "abone ol", "Altyaz" ve zamir kaynaklı "10'u/10'a/10'un" sayısı 0. Doğru "saat 10'da" (1) ve "yüz yüze" (6) korundu. 13 kaydın metni güncellendi, ikinci çalıştırmada 0 değişiklik. 37 yeni test eklendi.
  - (−) Kural listesi elle tutuluyor; Whisper'ın başka halüsinasyonları yakalanmaz. Tek başına "ona kadar say" (ona = 10'a) artık sayıya çevrilmiyor; bu kabul edilen bir hata yönü, çünkü PII için önemli olan uzun rakam dizileri ve onlar bu kuraldan etkilenmiyor.
- **Rejected alternatives:**
  - Halüsinasyonları `no_speech_prob` / `avg_logprob` eşikleriyle elemek: daha genel bir yöntem, ama segment metadatası şu an saklanmıyor ve eşik gerçek veriyle kalibre edilmeli. Not edildi.
  - Ham transkripti filtrelemek: delili yok eder ve geri alınamaz.
- **Revisit if:** Daha fazla gerçek veri gelirse, `no_speech_prob` saklanıp eşik tabanlı filtreye geçilir.

### ADR-36. Gerçek çalıştırmadan kalan son açıklar: isimler, "10 bin", HTTP 429
- **Status:** Accepted (2026-11-08).
- **Context:** README'de "açık" diye listelenen üç madde vardı: listede olmayan ilk adlar, "on bin" → "10 1000", ve 429'un deneme hakkı tüketmesi.
- **Decision:**
  1. **İsimler**, dört kuralla:
     - (a) Ad listesi yaklaşık 190'dan 400'ü aşkın ada çıktı; nehir, yaprak, umut gibi ortak kelime olan adlar AMBIGUOUS listesine kondu.
     - (b) **İsim sorusuna cevap:** Önceki konuşma sırası "adınızı / soyadınızı / isminizi" soruyorsa ve cevap yalnız 1–4 büyük harfli kelimeden oluşuyorsa (sonunda "'um" gibi bir ek olabilir), bu kelimeler isim sayılıyor. Listede olmasalar da maskeleniyor ("Çınar Kaplan", "Melda Özkul'um").
     - (c) **Bitişiklik:** Tespit edilmiş bir ismin hemen önündeki bilinen ilk ad, ismin parçası sayılıyor; cümle başında belirsiz olsa bile.
     - (d) **Soyadlarının ayrımı:** Gündelik kelime olan soyadları (Kaya, Demir, Yıldız…) cümle başında kelime olarak okunuyor. Ayırt edici soyadları (Özbek, Özkul…) her yerde isim sayılıyor.
     - "Bey/Hanım"dan önceki, cümle başında olmayan büyük harfli kelime de ad sayılıyor.
  2. **"10 bin dakika":** STT miktarı rakamla, ölçeği kelimeyle yazıyor. 1–999 arası bir sayı (başında 0 yok, ondalık değil) + bin/milyon/milyar birleştiriliyor; ardından gelen küçük sayı da ekleniyor ("2 bin 500" → 2500). "0532 bin" bir telefon grubu olarak bırakılıyor.
  3. **HTTP 429:** Sağlayıcının "bekle" demesi başarısız bir deneme değil. `*_rate_limited` hatası `Retry-After` kadar (+0–2 sn jitter) sonra yeniden kuyruğa giriyor ve deneme hakkı **geri veriliyor**. Sonsuz beklemeye karşı adım başına ayrı bir sınır var: `MAX_THROTTLE_WAITS = 30`. Bu sınır aşılırsa artık normal deneme sayılıyor ve kayıt görünür biçimde FAILED oluyor. Her bekleme `throttled` olayı olarak kaydediliyor.
- **Consequences:**
  - (+) Gerçek veriyle sıfırdan yapılan son çalıştırmada (`down -v` ardından `up`) 22 rate-limit beklemesi oldu ve 19/19 kayıt elle `reprocess` gerekmeden tamamlandı.
  - (+) İsim taraması iki turda yapıldı: yeni kurallar eklendi, ardından `remask` uygulandı. Son durumda listedeki isimlerden açıkta kalan tek kelime "Yıldız"; o da iki şirket adının ilk kelimesi olarak.
  - (+) CRM'deki 7 şikâyet kaydında e-posta yok. Testler: 403 passed.
  - (−) İsim tespiti hâlâ kural tabanlı. Listede olmayan, isim sorusuna cevap olarak da verilmeyen ve "Bey/Hanım" bağlamı da olmayan bir ad kaçabilir. Bu, NER olmadan kapatılamayan bir sınır; aşağıdaki "Revisit if" maddesine bakın.
- **Rejected alternatives:**
  - Her büyük harfli kelimeyi maskelemek: Harbor, WhatsApp, gün adları ve şirket adları gibi yüzlerce kelimeyi yok ederdi.
  - 429'u sınırsız beklemek: günlük kota bittiğinde kayıt sonsuza kadar "işleniyor" görünürdü.
- **Revisit if:** Gerçek veri hacmi artarsa Türkçe bir NER modeli (ör. BERTurk tabanlı) MASKING adımına ikinci katman olarak eklenir; kural tabanlı katman kalır.

---

## Assumptions

- Göreli zamanlar `recorded_at`'e göre çözülüyor; saat dilimi `Europe/Istanbul` (UTC+3).
- Toplantı 1 saatlik tek bir slot. "Sonraki en erken slot" aynı günle sınırlı değil.
- Bir çağrıda en fazla bir toplantı ve bir şikâyet olduğu varsayılıyor.
- CRM yapılandırması tenant başına. Mock yalnız Harbor'u temsil ediyor. `call_id` ham kayıt id'si; tenant ayrımı nottaki `tenant=` etiketiyle yapılıyor.
- Byte-özdeş ses, aynı çağrının tekrar yüklenmesi demek.
- Stereo kanallar konuşmacı bazında ayrık (ölçüldü). Hangi kanalın müşteri olduğu içerikten çıkarılıyor.
- Şikâyet özeti ve rezervasyon notu PII içermiyor (maskeli metinden üretiliyor).

## Not done (and why)

- **Gerçek Groq çağrısı yapılmadı.** Geliştirme ortamında anahtar yoktu. Türkçe STT kalitesi, rol tespiti, PII recall'ı ve gerçek 21 kaydın `list-actions` çıktısı ölçülmedi. Anahtar ile ilk yapılması gereken: `docker compose up`, ardından `list-actions` ve `get-transcript` çıktılarını elle kontrol etmek.
- **HTTP API yok** (ADR-31).
- **Rezervasyon sırası `recorded_at`'e göre değil** (ADR-26); bunun için kayıtlar arasında bir bariyer gerekiyor.
- **Süreçler arası ortak bir rate limiter yok** (ADR-28).
- **Tenant başına veritabanı rolü yok** (ADR-30).
- **Anahtar rotasyonu yok** (ADR-20).
- **faster-whisper** yalnız arayüz olarak var; gerçek bir modelle çalıştırılmadı.
- İçerik düzeyinde tekilleştirme (aynı müşterinin farklı bir ses dosyasıyla tekrar araması) kapsam dışı.
