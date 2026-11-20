"""Common Turkish first names and surnames for name detection.

AMBIGUOUS names are also everyday words ("umut", "deniz", "barış"); they are only
masked when the context says they are a name (mid-sentence capital, "adım ...",
"... Hanım") so that "umut ediyorum" stays readable.
"""

FIRST_NAMES = frozenset("""
Ahmet Mehmet Mustafa Ali Hüseyin Hasan İbrahim İsmail Osman Yusuf Murat Ömer Ramazan
Halil Süleyman Abdullah Mahmut Recep Kemal Fatih Emre Burak Serkan Hakan Kadir Cem Can
Onur Selim Sinan Tolga Volkan Kerem Mert Berk Arda Emir Eren Efe Kaan Baran Deniz Umut
Barış Oğuz Orhan Erkan Erdem Levent Gökhan Tuncay Taner Cengiz Engin Bülent Cihan Ercan
Yavuz Yasin Talha Furkan Enes Batuhan Alper Anıl Doğan Ozan Uğur Tarık Serdar Sercan
Sedat Şahin Kenan Metin Nihat Rıza Zeki Necati Erol İlker Koray Tamer Tayfun Turgut Ayhan
Ayşe Fatma Emine Hatice Zeynep Elif Meryem Şerife Zehra Sultan Hanife Merve Esra Özlem
Yasemin Büşra Kübra Gizem Derya Seda Ebru Burcu Ece Selin Melis Dilek Aslı Pınar Gamze
Tuğba Sibel Songül Gülsüm Hacer Hülya Nur Nurcan Leyla Sevgi Sevim Filiz Canan Serap
Aylin Damla Duygu Eda Ezgi Gül Gülay Hande İrem Melek Nazlı Neslihan Nesrin Özge Rabia
Sinem Şeyma Tülay Yeliz Zerrin Bahar Yağmur Ceren Cansu Defne Ela Asya Azra Eylül İpek
Buse Beyza Dilara Elçin Feyza Hilal Kader Lale Meltem Nilay Nihan Oya Pelin Sevda Simge
Şule Tuba Umay Ayten Belgin Cemile Dilan Esin Figen Gonca Handan Işıl Jale Kezban Mine
Yunus Eyüp Yakup Musa İsa Davut Hamza Bilal Selçuk Serhat Sefa Safa Samet Semih Sezgin
Soner Şükrü Taylan Tuncer Ufuk Ümit Vedat Yiğit Yücel Zafer Adem Akif Alp Atakan Aykut
Aytaç Bahadır Bekir Berkay Bora Cafer Celal Cevdet Çağatay Çağrı Doğukan Ekrem Emin
Emrah Ersin Ertan Ertuğrul Faruk Ferhat Fikret Gökay Güray Hayati Hilmi Hikmet İlhan
İlyas İsmet Kamil Kazım Kürşat Mesut Mithat Muammer Muharrem Mücahit Necmi Nevzat Nuri
Oktay Okan Orçun Rahmi Rasim Rüştü Sabri Sadık Salih Sami Savaş Şenol Şevket Tahsin
Tevfik Tuğrul Tunahan Utku Veli Yahya Yaşar Zekeriya Arif Aziz Caner Cemal Cüneyt Ender
Erhan Evren Fırat Gürkan Halit İhsan Kerim Kutay Melih Oğuzhan Rıdvan Selahattin Tufan
Ulaş Yener Tuna Doruk Çınar Aras Kuzey Miraç Poyraz Ege Atlas Rüzgar
Tuğçe Aysel Aynur Ayşegül Berna Betül Birsen Burçin Çiğdem Elvan Emel Esma Fadime Feride
Gülcan Gülşen Gülten Havva Hayriye Hümeyra İlknur İnci Kevser Medine Melike Meral Nazan
Necla Nermin Nilgün Nuray Nurten Perihan Reyhan Rukiye Saadet Sabiha Safiye Saliha Selda
Selma Semra Serpil Sevil Sevinç Süheyla Şükran Tülin Türkan Ümran Zübeyde Zuhal Arzu
Aslıhan Ayla Begüm Beril Ceyda Cemre Dicle Funda Gökçe Gülnur Hazal İlayda Irmak
Kardelen Melda Naz Nehir Öykü Seçil Serra Tuana Yaprak Zeren Ecrin Miray Nisa Sude
""".split())

AMBIGUOUS = frozenset("""
Deniz Umut Barış Can Nur Gül Bahar Yağmur Sevgi Doğan Şahin Kader Lale Melek Sultan Ela
Ece Efe Eren Emir Kaan Asya Defne Ezgi İpek Oya Sevim Ayten Kenan Onur Selim Cem Ozan
Tuna Doruk Çınar Kuzey Poyraz Ege Atlas Rüzgar Irmak Yaprak Naz Nehir İnci Arzu Ümit
Zafer Savaş Alp Bora Fırat Ulaş Musa İsa Veli Aziz Emin Sami Salih Arif
""".split())

SURNAMES = frozenset("""
Yılmaz Kaya Demir Şahin Çelik Yıldız Yıldırım Öztürk Aydın Özdemir Arslan Doğan Kılıç
Aslan Çetin Kara Koç Kurt Özkan Şimşek Polat Özcan Korkmaz Çakır Erdoğan Yavuz Can
Acar Şen Aktaş Güler Yalçın Güneş Bozkurt Bulut Keskin Ünal Turan Gül Özer Işık Kaplan
Avcı Sarı Tekin Taş Köse Yüksel Ateş Aksoy Akgül Akın Karaca Kahraman Uçar Coşkun Tunç
Erdem Duman Özgür Çiftçi Kocabaş Altun Türk Uysal Toprak Bayram Durmaz Sezer Aktürk
Demirel Özbek Karakaya Kılınç Akyüz Altıntaş Aydoğan Akbulut Akdemir Akkaya Aksu Altay
Arıkan Bilgin Ekinci Gündoğdu Gürbüz Karabulut Karadeniz Oral Önal Özçelik Özgün Soylu
Taşkın Türkmen Yazıcı Yiğit Yücel Erbil Özkul Kurtuluş Akar Tuncer Aslantaş Bayraktar
Çakmak Demirci Dinç Dursun Erçelik Ergün Gökmen Gümüş Karakuş Kaymak Kırmızı Mutlu
Özden Sönmez Tatlı Tosun Ünlü Yaman Yazgan Yener Yurt Zorlu
""".split())

# Surnames that are also everyday words ("kaya" = rock, "demir" = iron): at the start of a
# sentence they are read as the word. Distinctive surnames ("Özbek") are names anywhere.
SURNAME_WORDS = frozenset("""
Kaya Demir Şahin Çelik Yıldız Yıldırım Arslan Doğan Kılıç Aslan Kara Koç Kurt Şimşek
Korkmaz Çakır Yavuz Can Acar Şen Güler Güneş Bulut Keskin Turan Gül Özer Işık Kaplan Avcı
Sarı Tekin Taş Köse Yüksel Ateş Uçar Coşkun Tunç Erdem Duman Özgür Çiftçi Altun Türk Uysal
Toprak Bayram Durmaz Sezer Akın Kurtuluş Çakmak Dinç Gümüş Kaymak Kırmızı Mutlu Sönmez
Tatlı Tosun Ünlü Yurt Zorlu Akar Karaca Kahraman Güler Polat Aydın
""".split())

# Capitalised words that look like names but are not people.
NOT_NAMES = frozenset("""
Harbor Bey Hanım Bay Bayan Sayın Merhaba Alo Evet Hayır Tamam Teşekkürler Teşekkür
İyi Günler Peki Şey Efendim Türkiye İstanbul Ankara İzmir Pazartesi Salı Çarşamba
Perşembe Cuma Cumartesi Pazar Ocak Şubat Mart Nisan Mayıs Haziran Temmuz Ağustos Eylül
Ekim Kasım Aralık Temsilci Müşteri
""".split())
