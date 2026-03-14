"""
╔══════════════════════════════════════════════════════════════════╗
║           MAKTK.IS — Telegram Teknik Analiz Botu v2             ║
║  RSI • MACD • MA • Bollinger • Hacim • Hafıza • Confluence      ║
║  Anomali • Pattern • Haftalık Özet • Senaryo Analizi            ║
╚══════════════════════════════════════════════════════════════════╝

KURULUM:
────────
1. Python 3.8+ kur → https://python.org/downloads
2. Terminalde:
   pip install python-telegram-bot yfinance pandas pandas-ta schedule
3. Telegram'da @BotFather → /newbot → token al
4. Telegram'da @userinfobot → Chat ID öğren
5. TELEGRAM_TOKEN ve CHAT_ID alanlarını doldur
6. python maktk_bot_v2.py
"""

import asyncio
import logging
import threading
import time
from collections import Counter
from datetime import datetime, timedelta

import pandas as pd
import pandas_ta as ta
import schedule
import yfinance as yf
from telegram import Bot
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

# ════════════════════════════════════════════════════════
# ⚙️  AYARLAR — SADECE BU KISMI DEĞİŞTİR
# ════════════════════════════════════════════════════════

TELEGRAM_TOKEN = "BURAYA_BOT_TOKEN_YAZ"
CHAT_ID        = "BURAYA_CHAT_ID_YAZ"

SEMBOL = "MAKTK.IS"

RSI_ASIRI_SATIM    = 35
RSI_ASIRI_ALIM     = 65
FIYAT_ALARM_YUKARI = 0.0
FIYAT_ALARM_ASAGI  = 0.0
GUNLUK_RAPOR_SAATI = "09:05"
HAFTALIK_OZET_GUNU = "friday"   # schedule için İngilizce gün adı
HAFTALIK_OZET_SAAT = "18:00"
KONTROL_SIKLIGI_DK = 15

# Fiyat hafızası tolerans %
SEVIYE_TOLERANS_PCT = 1.5
ILERIYE_BAK_GUN     = 5
MIN_TEST_SAYISI     = 2

# Anomali: hacim ortalamanın kaç katı olunca tetiklensin
ANOMALI_HACIM_KATI  = 3.0

# Confluence: kaç sinyal aynı anda olunca güçlü say
CONFLUENCE_ESIK     = 3

# ════════════════════════════════════════════════════════
# 📋  LOGGING & DURUM TAKİBİ
# ════════════════════════════════════════════════════════

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

son_sinyaller: dict = {
    "rsi_asiri_satim_1d": False, "rsi_asiri_satim_4h": False,
    "rsi_asiri_satim_1h": False, "rsi_asiri_satim_15m": False,
    "macd_kesisim_al_1d": False, "macd_kesisim_al_4h": False,
    "macd_kesisim_al_1h": False, "macd_kesisim_al_15m": False,
    "macd_kesisim_sat_1d": False,"macd_kesisim_sat_4h": False,
    "macd_kesisim_sat_1h": False,"macd_kesisim_sat_15m": False,
    "fiyat_alarm_yukari": False,  "fiyat_alarm_asagi": False,
    "anomali": False,
    "confluence_al": False,       "confluence_sat": False,
}

# Haftalık sinyal geçmişi (istatistik için)
haftalik_sinyal_gecmisi: list[dict] = []

# Senaryo mesajı için kullanıcıya özel durum
senaryo_bekliyor: dict = {}   # chat_id → True/False


# ════════════════════════════════════════════════════════
# 📊  VERİ & TEMEL İNDİKATÖRLER
# ════════════════════════════════════════════════════════

def veri_cek(interval: str, period: str) -> pd.DataFrame | None:
    try:
        df = yf.download(SEMBOL, period=period, interval=interval, progress=False)
        if df.empty or len(df) < 30:
            logger.warning(f"Yetersiz veri: {interval}")
            return None
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
        return df
    except Exception as e:
        logger.error(f"Veri hatası ({interval}): {e}")
        return None


def indiktorleri_hesapla(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["RSI"]       = ta.rsi(df["Close"], length=14)
    macd            = ta.macd(df["Close"], fast=12, slow=26, signal=9)
    if macd is not None:
        df["MACD"]        = macd.get("MACD_12_26_9")
        df["MACD_signal"] = macd.get("MACDs_12_26_9")
        df["MACD_hist"]   = macd.get("MACDh_12_26_9")
    df["MA20"]      = ta.sma(df["Close"], length=20)
    df["MA50"]      = ta.sma(df["Close"], length=50)
    bb              = ta.bbands(df["Close"], length=20, std=2)
    if bb is not None:
        df["BB_upper"] = bb.get("BBU_20_2.0")
        df["BB_mid"]   = bb.get("BBM_20_2.0")
        df["BB_lower"] = bb.get("BBL_20_2.0")
    df["Hacim_MA20"] = ta.sma(df["Volume"], length=20)
    return df


def _f(series_val) -> float | None:
    try:
        v = float(series_val)
        return v if pd.notna(v) else None
    except Exception:
        return None


def sinyal_uret(df: pd.DataFrame, interval: str) -> dict:
    son     = df.iloc[-1]
    onceki  = df.iloc[-2]
    fiyat   = _f(son["Close"])

    s = dict(
        fiyat=fiyat,
        rsi=_f(son.get("RSI")),
        macd=_f(son.get("MACD")),
        macd_signal=_f(son.get("MACD_signal")),
        macd_hist=_f(son.get("MACD_hist")),
        ma20=_f(son.get("MA20")),
        ma50=_f(son.get("MA50")),
        bb_upper=_f(son.get("BB_upper")),
        bb_lower=_f(son.get("BB_lower")),
        hacim=_f(son["Volume"]),
        hacim_ma20=_f(son.get("Hacim_MA20")),
        rsi_asiri_satim=False, rsi_asiri_alim=False,
        macd_kesisim_al=False, macd_kesisim_sat=False,
        bb_alt_dokunu=False,   bb_ust_dokunu=False,
        yuksek_hacim=False,    trend="YATAY",
    )

    if s["rsi"]:
        s["rsi_asiri_satim"] = s["rsi"] < RSI_ASIRI_SATIM
        s["rsi_asiri_alim"]  = s["rsi"] > RSI_ASIRI_ALIM

    om = _f(onceki.get("MACD")); osg = _f(onceki.get("MACD_signal"))
    if s["macd"] and s["macd_signal"] and om and osg:
        s["macd_kesisim_al"]  = om < osg  and s["macd"] > s["macd_signal"]
        s["macd_kesisim_sat"] = om > osg  and s["macd"] < s["macd_signal"]

    if s["bb_lower"] and fiyat: s["bb_alt_dokunu"] = fiyat <= s["bb_lower"]
    if s["bb_upper"] and fiyat: s["bb_ust_dokunu"] = fiyat >= s["bb_upper"]
    if s["hacim"] and s["hacim_ma20"]:
        s["yuksek_hacim"] = s["hacim"] > s["hacim_ma20"] * 1.5

    if s["ma20"] and s["ma50"]:
        s["trend"] = "YÜKSELİŞ 📈" if s["ma20"] > s["ma50"] else "DÜŞÜŞ 📉"

    return s


def interval_adi(iv: str) -> str:
    return {"1d":"Günlük","4h":"4 Saatlik","1h":"1 Saatlik","15m":"15 Dakikalık"}.get(iv, iv)

def interval_period(iv: str) -> str:
    return {"1d":"6mo","4h":"60d","1h":"30d","15m":"8d"}.get(iv, "1mo")


# ════════════════════════════════════════════════════════
# 🎯  1. CONFLUENCE SKORU
# ════════════════════════════════════════════════════════

def confluence_skoru_hesapla(df2y: pd.DataFrame) -> dict:
    """
    Tüm zaman dilimlerinden AL/SAT sinyallerini topla,
    destek/direnç yakınlığı ve hacimi de ekle → toplam puan.
    """
    al_puan  = 0
    sat_puan = 0
    detaylar = []

    for interval in ["1d", "4h", "1h", "15m"]:
        df = veri_cek(interval, interval_period(interval))
        if df is None:
            continue
        df = indiktorleri_hesapla(df)
        s  = sinyal_uret(df, interval)
        ad = interval_adi(interval)

        if s["rsi_asiri_satim"]:
            al_puan += 1
            detaylar.append(f"✅ RSI aşırı satım [{ad}]")
        if s["rsi_asiri_alim"]:
            sat_puan += 1
            detaylar.append(f"⛔ RSI aşırı alım [{ad}]")
        if s["macd_kesisim_al"]:
            al_puan += 1
            detaylar.append(f"✅ MACD AL kesişimi [{ad}]")
        if s["macd_kesisim_sat"]:
            sat_puan += 1
            detaylar.append(f"⛔ MACD SAT kesişimi [{ad}]")
        if s["bb_alt_dokunu"]:
            al_puan += 1
            detaylar.append(f"✅ Bollinger alt bant [{ad}]")
        if s["bb_ust_dokunu"]:
            sat_puan += 1
            detaylar.append(f"⛔ Bollinger üst bant [{ad}]")
        if s["yuksek_hacim"] and s["rsi_asiri_satim"]:
            al_puan += 1
            detaylar.append(f"✅ Yüksek hacim + RSI satım [{ad}]")

    # Destek/direnç yakınlığı (günlük 2 yıllık)
    guncel = _f(df2y["Close"].iloc[-1])
    if guncel:
        yakin = yakin_seviyeler_bul(df2y, guncel)
        for sv in yakin[:2]:
            ist = sv.get("istatistik", {})
            if sv["tip"] == "DESTEK" and sv["fark_pct"] <= SEVIYE_TOLERANS_PCT:
                if ist.get("yukari_ihtimal", 0) >= 60:
                    al_puan += 2
                    detaylar.append(f"✅ Güçlü destek ({sv['seviye']:.2f}₺, %{ist['yukari_ihtimal']} yukarı)")
            if sv["tip"] == "DİRENÇ" and sv["fark_pct"] <= SEVIYE_TOLERANS_PCT:
                if ist.get("asagi_ihtimal", 0) >= 60:
                    sat_puan += 2
                    detaylar.append(f"⛔ Güçlü direnç ({sv['seviye']:.2f}₺, %{ist['asagi_ihtimal']} aşağı)")

    toplam = al_puan + sat_puan
    return {
        "al_puan":   al_puan,
        "sat_puan":  sat_puan,
        "toplam":    toplam,
        "detaylar":  detaylar,
        "fiyat":     guncel,
    }


def confluence_mesaji(c: dict) -> str:
    al  = c["al_puan"]
    sat = c["sat_puan"]
    f   = c["fiyat"]

    if al >= CONFLUENCE_ESIK and al > sat:
        baslik = f"🔥 <b>GÜÇLÜ AL SİNYALİ — Confluence {al}/{al+sat}</b>"
    elif sat >= CONFLUENCE_ESIK and sat > al:
        baslik = f"🔥 <b>GÜÇLÜ SAT SİNYALİ — Confluence {sat}/{al+sat}</b>"
    elif al > sat:
        baslik = f"🟡 <b>ZAYIF AL — Confluence {al}/{al+sat}</b>"
    elif sat > al:
        baslik = f"🟡 <b>ZAYIF SAT — Confluence {sat}/{al+sat}</b>"
    else:
        baslik = f"⚪ <b>NÖTR — Confluence {al}/{al+sat}</b>"

    detay_str = "\n".join(f"  {d}" for d in c["detaylar"]) or "  Sinyal yok"
    return (
        f"{baslik}\n"
        f"📌 MAKTK | Fiyat: <b>{f:.2f} ₺</b>\n\n"
        f"<b>Aktif Sinyaller:</b>\n{detay_str}\n\n"
        f"⚠️ <i>Yatırım tavsiyesi değildir.</i>"
    )


# ════════════════════════════════════════════════════════
# 🚨  2. HAClM ANOMALİ DEDEKTÖRü
# ════════════════════════════════════════════════════════

def anomali_kontrol(df: pd.DataFrame) -> dict | None:
    """Son mum hacmi ortalamanın ANOMALI_HACIM_KATI katını geçti mi?"""
    if len(df) < 21:
        return None
    son_hacim  = _f(df["Volume"].iloc[-1])
    ort_hacim  = _f(df["Hacim_MA20"].iloc[-1])
    if not son_hacim or not ort_hacim or ort_hacim == 0:
        return None
    kat = son_hacim / ort_hacim
    if kat < ANOMALI_HACIM_KATI:
        return None

    son_fiyat  = _f(df["Close"].iloc[-1])
    onceki_fiy = _f(df["Close"].iloc[-2])
    fiyat_degisim = ((son_fiyat - onceki_fiy) / onceki_fiy * 100) if onceki_fiy else 0

    return {
        "kat":           round(kat, 1),
        "son_hacim":     int(son_hacim),
        "ort_hacim":     int(ort_hacim),
        "fiyat":         son_fiyat,
        "fiyat_degisim": round(fiyat_degisim, 2),
    }


def anomali_mesaji(a: dict) -> str:
    yon = "📈 YÜKSELİŞ" if a["fiyat_degisim"] > 0 else "📉 DÜŞÜŞ"
    return (
        f"🚨 <b>HAClM ANOMALİSİ — MAKTK</b>\n"
        f"💹 Fiyat: <b>{a['fiyat']:.2f} ₺</b>  ({a['fiyat_degisim']:+.2f}%)\n\n"
        f"📊 Hacim: <b>{a['son_hacim']:,}</b>\n"
        f"📊 Ort. Hacim (20g): {a['ort_hacim']:,}\n"
        f"⚡ Oran: <b>{a['kat']}x normalin üzerinde!</b>\n\n"
        f"Yön: {yon}\n\n"
        f"💡 Büyük oyuncu hareketi veya önemli haber olabilir.\n"
        f"⚠️ <i>Yatırım tavsiyesi değildir.</i>"
    )


# ════════════════════════════════════════════════════════
# 🔁  3. FİYAT DAVRANIŞ ÖRÜNTÜSÜ (PATTERN)
# ════════════════════════════════════════════════════════

def pattern_analiz(df: pd.DataFrame, gecmis_gun: int = 3) -> dict:
    """
    Son gecmis_gun günlük fiyat hareketinin (yukarı/aşağı dizisi)
    geçmişte kaç kez tekrarlandığını ve sonrasında ne olduğunu analiz et.
    """
    kapanislar = df["Close"].values
    n = len(kapanislar)
    if n < gecmis_gun + 5:
        return {"bulunan": 0}

    # Son N günün hareket dizisi (U=yukarı, D=aşağı)
    def hareket_dizisi(idx: int, uzunluk: int) -> str:
        dizi = ""
        for i in range(idx - uzunluk, idx):
            dizi += "U" if kapanislar[i+1] > kapanislar[i] else "D"
        return dizi

    son_dizi = hareket_dizisi(n - 1, gecmis_gun)

    eslesme_sonrasi = []
    for i in range(gecmis_gun, n - 2):
        if hareket_dizisi(i, gecmis_gun) == son_dizi:
            # Sonraki gün hareketi
            sonraki = float(kapanislar[i + 1])
            bugunki = float(kapanislar[i])
            degisim = (sonraki - bugunki) / bugunki * 100
            eslesme_sonrasi.append(round(degisim, 2))

    if not eslesme_sonrasi:
        return {"bulunan": 0, "dizi": son_dizi}

    yukari = [x for x in eslesme_sonrasi if x > 0]
    asagi  = [x for x in eslesme_sonrasi if x <= 0]
    toplam = len(eslesme_sonrasi)

    return {
        "bulunan":        toplam,
        "dizi":           son_dizi,
        "yukari_sayi":    len(yukari),
        "asagi_sayi":     len(asagi),
        "yukari_ihtimal": round(len(yukari) / toplam * 100, 1),
        "ort_yukari":     round(sum(yukari) / len(yukari), 2) if yukari else 0,
        "ort_asagi":      round(sum(asagi) / len(asagi), 2) if asagi else 0,
        "son_ornekler":   eslesme_sonrasi[-5:],
    }


def pattern_mesaji(p: dict, guncel_fiyat: float) -> str:
    if p["bulunan"] == 0:
        return f"🔁 <b>Pattern Analizi — MAKTK</b>\n\nGeçmişte eşleşen örüntü bulunamadı."

    dizi_str = " → ".join("📈" if c == "U" else "📉" for c in p["dizi"])
    yon_emoji = "📈" if p["yukari_ihtimal"] >= 50 else "📉"

    return (
        f"🔁 <b>Pattern Analizi — MAKTK</b>\n"
        f"💹 Güncel: <b>{guncel_fiyat:.2f} ₺</b>\n\n"
        f"<b>Son {len(p['dizi'])} gün dizisi:</b> {dizi_str}\n\n"
        f"📊 Geçmişte <b>{p['bulunan']} kez</b> aynı dizi görüldü\n"
        f"{yon_emoji} Ertesi gün yukarı: <b>%{p['yukari_ihtimal']}</b>  "
        f"({p['yukari_sayi']} kez, ort. +{p['ort_yukari']}%)\n"
        f"📉 Ertesi gün aşağı: <b>%{100 - p['yukari_ihtimal']:.1f}</b>  "
        f"({p['asagi_sayi']} kez, ort. {p['ort_asagi']}%)\n\n"
        f"<i>Son örnekler: {', '.join(f'{x:+.1f}%' for x in p['son_ornekler'])}</i>\n\n"
        f"⚠️ <i>Yatırım tavsiyesi değildir.</i>"
    )


# ════════════════════════════════════════════════════════
# 📅  4. HAFTALIK PERFORMANS ÖZETİ
# ════════════════════════════════════════════════════════

def haftalik_ozet_olustur() -> str:
    df = veri_cek("1d", "1mo")
    if df is None:
        return "❌ Haftalık özet için veri alınamadı."

    df = indiktorleri_hesapla(df)
    bugun       = datetime.now()
    hafta_basi  = bugun - timedelta(days=bugun.weekday())   # Pazartesi

    # Bu haftanın günlük kapanışları
    hafta_df = df[df.index >= hafta_basi.strftime("%Y-%m-%d")]
    if hafta_df.empty:
        hafta_df = df.tail(5)

    acilis      = _f(hafta_df["Open"].iloc[0])
    kapanis     = _f(hafta_df["Close"].iloc[-1])
    haftalik_dk = ((kapanis - acilis) / acilis * 100) if acilis else 0
    yuksek      = float(hafta_df["High"].max())
    dusuk       = float(hafta_df["Low"].min())
    haftalik_hacim = float(hafta_df["Volume"].sum())

    # Bu hafta üretilen sinyalleri say
    al_sinyal  = sum(1 for sg in haftalik_sinyal_gecmisi if sg.get("yon") == "AL")
    sat_sinyal = sum(1 for sg in haftalik_sinyal_gecmisi if sg.get("yon") == "SAT")
    anomali_n  = sum(1 for sg in haftalik_sinyal_gecmisi if sg.get("tip") == "ANOMALİ")

    # RSI ve MACD son durum
    son_rsi  = _f(df["RSI"].iloc[-1])
    son_macd = _f(df["MACD"].iloc[-1])
    son_macd_s = _f(df["MACD_signal"].iloc[-1])
    macd_durum = "Yükseliş ↑" if (son_macd and son_macd_s and son_macd > son_macd_s) else "Düşüş ↓"

    dk_emoji = "📈" if haftalik_dk >= 0 else "📉"
    return (
        f"📅 <b>Haftalık Performans Özeti — MAKTK</b>\n"
        f"<i>{hafta_basi.strftime('%d.%m')} – {bugun.strftime('%d.%m.%Y')}</i>\n"
        f"{'─'*30}\n\n"
        f"{dk_emoji} <b>Haftalık Değişim: {haftalik_dk:+.2f}%</b>\n"
        f"  Açılış  : {acilis:.2f} ₺\n"
        f"  Kapanış : {kapanis:.2f} ₺\n"
        f"  En Yüksek: {yuksek:.2f} ₺\n"
        f"  En Düşük : {dusuk:.2f} ₺\n"
        f"  Haftalık Hacim: {haftalik_hacim:,.0f}\n\n"
        f"📊 <b>İndikatör Durumu:</b>\n"
        f"  RSI   : {son_rsi:.1f if son_rsi else '—'}\n"
        f"  MACD  : {macd_durum}\n\n"
        f"🤖 <b>Bu Hafta Bot Ürettiği Sinyaller:</b>\n"
        f"  🟢 AL sinyali  : {al_sinyal}\n"
        f"  🔴 SAT sinyali : {sat_sinyal}\n"
        f"  🚨 Hacim anomali: {anomali_n}\n\n"
        f"⚠️ <i>Yatırım tavsiyesi değildir.</i>"
    )


# ════════════════════════════════════════════════════════
# 🔮  5. SENARYO ANALİZİ
# ════════════════════════════════════════════════════════

def senaryo_analiz(df: pd.DataFrame, hedef_fiyat: float) -> str:
    """
    'Fiyat X'e çıkarsa ne olur?' sorusunu yanıtla:
    - O seviyedeki geçmiş direnç gücü
    - RSI'nin o noktada muhtemel değeri
    - Hedefe kaç günde ulaşılabilir
    """
    guncel = _f(df["Close"].iloc[-1])
    if not guncel or not hedef_fiyat:
        return "❌ Geçersiz fiyat."

    degisim_pct = (hedef_fiyat - guncel) / guncel * 100
    yon = "yukarı 📈" if degisim_pct > 0 else "aşağı 📉"

    # Geçmişte günlük değişim dağılımı
    kapanislar = df["Close"].values
    gunluk_getiriler = [(kapanislar[i+1] - kapanislar[i]) / kapanislar[i] * 100
                        for i in range(len(kapanislar)-1)]
    ort_gunluk = sum(abs(x) for x in gunluk_getiriler) / len(gunluk_getiriler) if gunluk_getiriler else 1
    tahmini_gun = abs(degisim_pct) / ort_gunluk if ort_gunluk > 0 else 0

    # O seviyedeki geçmiş direnç/destek gücü
    seviyeler   = destek_direnc_bul(df, SEVIYE_TOLERANS_PCT)
    yakin_seviye = None
    min_fark = 999
    for sv in seviyeler:
        fark = abs(sv["seviye"] - hedef_fiyat) / hedef_fiyat * 100
        if fark < min_fark:
            min_fark = fark
            yakin_seviye = sv

    # Tahmini RSI hedefte
    mevcut_rsi = _f(df["RSI"].iloc[-1])
    tahmini_rsi = None
    if mevcut_rsi:
        # Basit lineer tahmini: yükselen fiyat → artan RSI
        tahmini_rsi = min(100, max(0, mevcut_rsi + degisim_pct * 0.8))

    satirlar = [
        f"🔮 <b>Senaryo Analizi — MAKTK</b>\n",
        f"💹 Güncel Fiyat : <b>{guncel:.2f} ₺</b>",
        f"🎯 Hedef Fiyat  : <b>{hedef_fiyat:.2f} ₺</b>",
        f"📐 Hareket      : {degisim_pct:+.2f}% ({yon})\n",
        f"⏱ <b>Tahmini Süre:</b> ~{tahmini_gun:.0f} işlem günü",
        f"   (Geçmiş ort. günlük hareket: %{ort_gunluk:.2f})\n",
    ]

    if tahmini_rsi:
        rsi_yorum = ""
        if tahmini_rsi > 70:
            rsi_yorum = " ⚠️ Aşırı alım bölgesi!"
        elif tahmini_rsi < 30:
            rsi_yorum = " ⚠️ Aşırı satım bölgesi!"
        satirlar.append(f"📊 <b>Tahmini RSI:</b> {tahmini_rsi:.1f}{rsi_yorum}\n")

    if yakin_seviye and min_fark <= 3.0:
        ist = seviye_gecmis_analiz(df, yakin_seviye["seviye"], SEVIYE_TOLERANS_PCT)
        tip_emoji = "🔴" if yakin_seviye["tip"] == "DİRENÇ" else "🟢"
        satirlar.append(
            f"{tip_emoji} <b>Hedef yakınında {yakin_seviye['tip']}:</b> "
            f"{yakin_seviye['seviye']:.2f} ₺"
        )
        if ist["toplam_temas"] > 0:
            satirlar.append(
                f"   {ist['toplam_temas']} kez test edildi → "
                f"Kırma: %{ist['yukari_ihtimal']} | Geri dönüş: %{ist['asagi_ihtimal']}"
            )
    else:
        satirlar.append("ℹ️ Hedef yakınında bilinen destek/direnç yok.")

    satirlar.append(f"\n⚠️ <i>Yatırım tavsiyesi değildir.</i>")
    return "\n".join(satirlar)


# ════════════════════════════════════════════════════════
# 🧠  FİYAT HAFIZASI (önceki versiyondan)
# ════════════════════════════════════════════════════════

def destek_direnc_bul(df: pd.DataFrame, tolerans_pct: float = 1.5) -> list[dict]:
    seviyeler = []
    n = len(df)
    for i in range(2, n - 2):
        yuksek = float(df["High"].iloc[i])
        dusuk  = float(df["Low"].iloc[i])
        if (df["High"].iloc[i] > df["High"].iloc[i-1] and df["High"].iloc[i] > df["High"].iloc[i-2]
                and df["High"].iloc[i] > df["High"].iloc[i+1] and df["High"].iloc[i] > df["High"].iloc[i+2]):
            seviyeler.append({"seviye": yuksek, "tip": "DİRENÇ", "tarih": df.index[i], "hacim": float(df["Volume"].iloc[i])})
        if (df["Low"].iloc[i] < df["Low"].iloc[i-1] and df["Low"].iloc[i] < df["Low"].iloc[i-2]
                and df["Low"].iloc[i] < df["Low"].iloc[i+1] and df["Low"].iloc[i] < df["Low"].iloc[i+2]):
            seviyeler.append({"seviye": dusuk, "tip": "DESTEK", "tarih": df.index[i], "hacim": float(df["Volume"].iloc[i])})

    if not seviyeler:
        return []
    seviyeler.sort(key=lambda x: x["seviye"])
    birlesik = []
    i = 0
    while i < len(seviyeler):
        grup = [seviyeler[i]]
        j = i + 1
        while j < len(seviyeler):
            if abs(seviyeler[j]["seviye"] - grup[0]["seviye"]) / grup[0]["seviye"] * 100 <= tolerans_pct:
                grup.append(seviyeler[j]); j += 1
            else:
                break
        tipler = [g["tip"] for g in grup]
        birlesik.append({
            "seviye":      round(sum(g["seviye"] for g in grup) / len(grup), 2),
            "tip":         "DİRENÇ" if tipler.count("DİRENÇ") >= tipler.count("DESTEK") else "DESTEK",
            "test_sayisi": len(grup),
            "hacim":       max(g["hacim"] for g in grup),
            "tarihler":    [g["tarih"] for g in grup],
        })
        i = j
    return [s for s in birlesik if s["test_sayisi"] >= MIN_TEST_SAYISI]


def seviye_gecmis_analiz(df: pd.DataFrame, seviye: float, tolerans_pct: float = 1.5) -> dict:
    yukari = []; asagi = []; temas = []
    for i in range(len(df) - ILERIYE_BAK_GUN - 1):
        fiyat = _f(df["Close"].iloc[i])
        if fiyat and abs(fiyat - seviye) / seviye * 100 <= tolerans_pct:
            gelecek = _f(df["Close"].iloc[i + ILERIYE_BAK_GUN])
            if gelecek:
                degisim = (gelecek - fiyat) / fiyat * 100
                temas.append({"tarih": str(df.index[i])[:10], "fiyat": fiyat, "degisim_pct": round(degisim, 2)})
                (yukari if degisim > 0 else asagi).append(degisim)
    toplam = len(yukari) + len(asagi)
    if not toplam:
        return {"toplam_temas": 0}
    return {
        "toplam_temas":    toplam,
        "yukari_sayisi":   len(yukari),
        "asagi_sayisi":    len(asagi),
        "yukari_ihtimal":  round(len(yukari) / toplam * 100, 1),
        "asagi_ihtimal":   round(len(asagi) / toplam * 100, 1),
        "ort_yukari_pct":  round(sum(yukari) / len(yukari), 2) if yukari else 0,
        "ort_asagi_pct":   round(sum(asagi) / len(asagi), 2) if asagi else 0,
        "temas_tarihleri": temas[-5:],
    }


def yakin_seviyeler_bul(df: pd.DataFrame, guncel_fiyat: float) -> list[dict]:
    seviyeler = destek_direnc_bul(df, SEVIYE_TOLERANS_PCT)
    yakin = []
    for s in seviyeler:
        fark_pct = abs(guncel_fiyat - s["seviye"]) / s["seviye"] * 100
        if fark_pct <= SEVIYE_TOLERANS_PCT * 3:
            ist = seviye_gecmis_analiz(df, s["seviye"], SEVIYE_TOLERANS_PCT)
            if ist["toplam_temas"] > 0:
                s["istatistik"] = ist
                s["fark_pct"]   = round(fark_pct, 2)
                yakin.append(s)
    return sorted(yakin, key=lambda x: x["fark_pct"])


def fiyat_hafizasi_mesaji(df: pd.DataFrame, guncel_fiyat: float) -> str:
    yakin = yakin_seviyeler_bul(df, guncel_fiyat)
    if not yakin:
        return ""
    satirlar = [f"🧠 <b>Fiyat Hafızası — MAKTK</b>\n💹 Güncel: <b>{guncel_fiyat:.2f} ₺</b>\n{'─'*28}"]
    for s in yakin[:4]:
        ist = s["istatistik"]
        emoji = "🔴" if s["tip"] == "DİRENÇ" else "🟢"
        yon_e = "📈" if ist["yukari_ihtimal"] >= 50 else "📉"
        uzaklik = (s["seviye"] - guncel_fiyat) / guncel_fiyat * 100
        satirlar.append(
            f"\n{emoji} <b>{s['tip']}: {s['seviye']:.2f} ₺</b>  ({uzaklik:+.1f}%)\n"
            f"  📊 {ist['toplam_temas']} temas  |  {s['test_sayisi']} pivot\n"
            f"  {yon_e} Yukarı: %{ist['yukari_ihtimal']}  |  Aşağı: %{ist['asagi_ihtimal']}\n"
            f"  Ort: ↑{ist['ort_yukari_pct']:+.1f}%  ↓{ist['ort_asagi_pct']:+.1f}%  ({ILERIYE_BAK_GUN}g)"
        )
        if ist["temas_tarihleri"]:
            son = ist["temas_tarihleri"][-1]
            satirlar.append(f"  🕒 Son: {son['tarih']} → {son['degisim_pct']:+.1f}%")
    satirlar.append("\n⚠️ <i>Yatırım tavsiyesi değildir.</i>")
    return "\n".join(satirlar)


def seviye_yakinlik_alarmi(df: pd.DataFrame, guncel_fiyat: float) -> str:
    yakin = yakin_seviyeler_bul(df, guncel_fiyat)
    if not yakin:
        return ""
    en_yakin = yakin[0]
    ist = en_yakin["istatistik"]
    if en_yakin["fark_pct"] > SEVIYE_TOLERANS_PCT or ist["toplam_temas"] < MIN_TEST_SAYISI:
        return ""
    yon  = "yaklaşıyor ⬆️" if guncel_fiyat < en_yakin["seviye"] else "test ediyor ⬇️"
    yon_e = "📈" if ist["yukari_ihtimal"] >= 50 else "📉"
    tip_e = "🔴" if en_yakin["tip"] == "DİRENÇ" else "🟢"
    return (
        f"⚡ <b>Seviye Alarmı — MAKTK</b>\n💹 Fiyat: <b>{guncel_fiyat:.2f} ₺</b>\n\n"
        f"{tip_e} <b>{en_yakin['tip']}: {en_yakin['seviye']:.2f} ₺</b> seviyesine {yon}\n"
        f"  Uzaklık: %{en_yakin['fark_pct']:.1f}\n\n"
        f"📊 <b>Geçmiş ({ist['toplam_temas']} temas):</b>\n"
        f"  {yon_e} Yukarı: %{ist['yukari_ihtimal']} ({ist['yukari_sayisi']} kez, ort. {ist['ort_yukari_pct']:+.1f}%)\n"
        f"  📉 Aşağı: %{ist['asagi_ihtimal']} ({ist['asagi_sayisi']} kez, ort. {ist['ort_asagi_pct']:+.1f}%)\n\n"
        f"⚠️ <i>Yatırım tavsiyesi değildir.</i>"
    )


# ════════════════════════════════════════════════════════
# 📨  ORTAK MESAJ FORMATI (ESKİ SİNYALLER)
# ════════════════════════════════════════════════════════

def sinyal_mesaji_olustur(s: dict, interval: str) -> str:
    zaman = interval_adi(interval)
    fiyat = s["fiyat"]
    mesajlar = []
    if s["rsi_asiri_satim"]:
        mesajlar.append(
            f"🟢 <b>AL — RSI Aşırı Satım</b> [{zaman}]\n"
            f"📌 MAKTK | Fiyat: <b>{fiyat:.2f} ₺</b>\n"
            f"📊 RSI: <b>{s['rsi']:.1f}</b>  Trend: {s['trend']}\n"
            f"{'🔥 Yüksek hacim!' if s['yuksek_hacim'] else ''}"
        )
    if s["rsi_asiri_alim"]:
        mesajlar.append(
            f"🔴 <b>SAT — RSI Aşırı Alım</b> [{zaman}]\n"
            f"📌 MAKTK | Fiyat: <b>{fiyat:.2f} ₺</b>\n"
            f"📊 RSI: <b>{s['rsi']:.1f}</b>  Trend: {s['trend']}"
        )
    if s["macd_kesisim_al"]:
        mesajlar.append(
            f"🟢 <b>AL — MACD Kesişimi</b> [{zaman}]\n"
            f"📌 MAKTK | Fiyat: <b>{fiyat:.2f} ₺</b>\n"
            f"📊 MACD > Sinyal  Trend: {s['trend']}\n"
            f"{'🔥 Yüksek hacim!' if s['yuksek_hacim'] else ''}"
        )
    if s["macd_kesisim_sat"]:
        mesajlar.append(
            f"🔴 <b>SAT — MACD Kesişimi</b> [{zaman}]\n"
            f"📌 MAKTK | Fiyat: <b>{fiyat:.2f} ₺</b>\n"
            f"📊 MACD < Sinyal  Trend: {s['trend']}"
        )
    return "\n\n".join(mesajlar)


def gunluk_rapor_olustur() -> str:
    simdi = datetime.now().strftime("%d.%m.%Y %H:%M")
    satirlar = [f"📋 <b>MAKTK Günlük Rapor</b> — {simdi}\n{'─'*30}"]
    for interval in ["1d", "4h", "1h", "15m"]:
        df = veri_cek(interval, interval_period(interval))
        if df is None: continue
        df = indiktorleri_hesapla(df)
        s  = sinyal_uret(df, interval)
        al  = sum([s["rsi_asiri_satim"], s["macd_kesisim_al"], s["bb_alt_dokunu"]])
        sat = sum([s["rsi_asiri_alim"], s["macd_kesisim_sat"], s["bb_ust_dokunu"]])
        ozet = ("🟢 GÜÇLÜ AL" if al >= 2 else "🟡 ZAYIF AL" if al == 1
                else "🔴 GÜÇLÜ SAT" if sat >= 2 else "🟡 ZAYIF SAT" if sat == 1 else "⚪ NÖTR")
        rsi_s  = f"{s['rsi']:.1f}"  if s["rsi"]  else "—"
        macd_s = f"{s['macd']:.3f}" if s["macd"] else "—"
        ma_s   = f"MA20:{s['ma20']:.2f} MA50:{s['ma50']:.2f}" if s["ma20"] and s["ma50"] else "—"
        satirlar.append(
            f"\n<b>{interval_adi(interval)}</b> | {ozet}\n"
            f"  Fiyat:{s['fiyat']:.2f}₺  RSI:{rsi_s}  MACD:{macd_s}\n"
            f"  {ma_s}  Trend:{s['trend']}"
        )
    satirlar.append(f"\n{'─'*30}\n⚠️ <i>Yatırım tavsiyesi değildir.</i>")
    return "\n".join(satirlar)


# ════════════════════════════════════════════════════════
# 🤖  TELEGRAM KOMUTLARI
# ════════════════════════════════════════════════════════

async def cmd_start(update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(
        "👋 <b>MAKTK Teknik Analiz Botu v2'ye Hoş Geldin!</b>\n\n"
        "📌 Sadece <b>MAKTK.IS</b> takip ediyorum.\n\n"
        "<b>Komutlar:</b>\n"
        "/analiz    — 4 zaman dilimi anlık analiz\n"
        "/confluence — Confluence skor (tüm sinyaller bir arada)\n"
        "/hafiza    — Fiyat hafızası & destek/direnç\n"
        "/pattern   — Fiyat davranış örüntüsü\n"
        "/senaryo   — 'Fiyat X'e çıkarsa ne olur?'\n"
        "/rapor     — Detaylı günlük rapor\n"
        "/fiyat     — Anlık fiyat\n"
        "/alarm     — Fiyat alarmaları\n"
        "/yardim    — Tüm komutlar"
    )


async def cmd_analiz(update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 Analiz yapılıyor...")
    mesajlar = []
    for interval in ["1d", "4h", "1h", "15m"]:
        df = veri_cek(interval, interval_period(interval))
        if df is None: continue
        df = indiktorleri_hesapla(df)
        s  = sinyal_uret(df, interval)
        al  = sum([s["rsi_asiri_satim"], s["macd_kesisim_al"], s["bb_alt_dokunu"]])
        sat = sum([s["rsi_asiri_alim"],  s["macd_kesisim_sat"], s["bb_ust_dokunu"]])
        ozet = "🟢 AL" if al > sat else ("🔴 SAT" if sat > al else "⚪ NÖTR")
        rsi_str = f"{s['rsi']:.1f}" if s["rsi"] else "—"
        mesajlar.append(
            f"<b>{interval_adi(interval)}</b> — {ozet}\n"
            f"  Fiyat: {s['fiyat']:.2f} ₺  |  RSI: {rsi_str}\n"
            f"  Trend: {s['trend']}"
        )
    await update.message.reply_html("📊 <b>MAKTK Anlık Analiz</b>\n" + "─"*28 + "\n" + "\n\n".join(mesajlar))


async def cmd_confluence(update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎯 Confluence skoru hesaplanıyor...")
    df2y = veri_cek("1d", "2y")
    if df2y is None:
        await update.message.reply_text("❌ Veri alınamadı.")
        return
    df2y = indiktorleri_hesapla(df2y)
    c = confluence_skoru_hesapla(df2y)
    await update.message.reply_html(confluence_mesaji(c))


async def cmd_hafiza(update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🧠 Fiyat hafızası analiz ediliyor...")
    df = veri_cek("1d", "2y")
    if df is None:
        await update.message.reply_text("❌ Veri alınamadı.")
        return
    df = indiktorleri_hesapla(df)
    guncel = _f(df["Close"].iloc[-1])
    mesaj = fiyat_hafizasi_mesaji(df, guncel)
    if mesaj:
        await update.message.reply_html(mesaj)
    else:
        await update.message.reply_text(f"ℹ️ {guncel:.2f} ₺ yakınında yeterli seviye bulunamadı.")


async def cmd_pattern(update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔁 Pattern analizi yapılıyor...")
    df = veri_cek("1d", "2y")
    if df is None:
        await update.message.reply_text("❌ Veri alınamadı.")
        return
    df = indiktorleri_hesapla(df)
    guncel = _f(df["Close"].iloc[-1])
    p = pattern_analiz(df, gecmis_gun=3)
    await update.message.reply_html(pattern_mesaji(p, guncel))


async def cmd_senaryo(update, context: ContextTypes.DEFAULT_TYPE):
    """Kullanıcıdan hedef fiyat ister."""
    chat_id = update.message.chat_id
    senaryo_bekliyor[chat_id] = True
    await update.message.reply_text(
        "🔮 Senaryo analizi için hedef fiyatı gir (₺):\n"
        "Örnek: 95.50"
    )


async def mesaj_isle(update, context: ContextTypes.DEFAULT_TYPE):
    """Senaryo için kullanıcının girdiği fiyatı işle."""
    chat_id = update.message.chat_id
    if not senaryo_bekliyor.get(chat_id):
        return
    senaryo_bekliyor[chat_id] = False
    try:
        hedef = float(update.message.text.replace(",", ".").strip())
    except ValueError:
        await update.message.reply_text("❌ Geçersiz fiyat. Örnek: 95.50")
        return
    await update.message.reply_text(f"🔮 {hedef:.2f} ₺ için senaryo analiz ediliyor...")
    df = veri_cek("1d", "2y")
    if df is None:
        await update.message.reply_text("❌ Veri alınamadı.")
        return
    df = indiktorleri_hesapla(df)
    sonuc = senaryo_analiz(df, hedef)
    await update.message.reply_html(sonuc)


async def cmd_rapor(update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📋 Rapor hazırlanıyor...")
    await update.message.reply_html(gunluk_rapor_olustur())


async def cmd_fiyat(update, context: ContextTypes.DEFAULT_TYPE):
    df = veri_cek("1d", "5d")
    if df is None:
        await update.message.reply_text("❌ Fiyat alınamadı.")
        return
    fiyat  = _f(df["Close"].iloc[-1])
    hacim  = float(df["Volume"].iloc[-1])
    yuksek = float(df["High"].iloc[-1])
    dusuk  = float(df["Low"].iloc[-1])
    await update.message.reply_html(
        f"💹 <b>MAKTK Anlık Fiyat</b>\n"
        f"  Fiyat  : <b>{fiyat:.2f} ₺</b>\n"
        f"  Yüksek : {yuksek:.2f} ₺\n"
        f"  Düşük  : {dusuk:.2f} ₺\n"
        f"  Hacim  : {hacim:,.0f}\n"
        f"  <i>{datetime.now().strftime('%H:%M:%S')}</i>"
    )


async def cmd_alarm(update, context: ContextTypes.DEFAULT_TYPE):
    yukari = f"{FIYAT_ALARM_YUKARI:.2f} ₺" if FIYAT_ALARM_YUKARI > 0 else "Ayarlı değil"
    asagi  = f"{FIYAT_ALARM_ASAGI:.2f} ₺"  if FIYAT_ALARM_ASAGI  > 0 else "Ayarlı değil"
    await update.message.reply_html(
        f"🔔 <b>Fiyat Alarmları — MAKTK</b>\n"
        f"  📈 Yukarı : {yukari}\n  📉 Aşağı  : {asagi}\n\n"
        f"<i>Değiştirmek için FIYAT_ALARM_YUKARI / FIYAT_ALARM_ASAGI değerlerini düzenle.</i>"
    )


async def cmd_yardim(update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(
        "📖 <b>Yardım — MAKTK Bot v2</b>\n\n"
        "/analiz     — RSI, MACD, Bollinger, MA (4 zaman dilimi)\n"
        "/confluence — Tüm sinyallerin birleşik gücü (1-10 puan)\n"
        "/hafiza     — Yakın destek/direnç + geçmiş istatistik\n"
        "/pattern    — Geçmiş fiyat dizisi örüntü analizi\n"
        "/senaryo    — Hedef fiyat senaryo analizi\n"
        "/rapor      — Detaylı günlük rapor\n"
        "/fiyat      — Anlık fiyat, yüksek, düşük, hacim\n"
        "/alarm      — Fiyat alarm seviyeleri\n\n"
        "<b>Otomatik Bildirimler:</b>\n"
        "• RSI aşırı satım/alım\n"
        "• MACD kesişimi\n"
        "• Confluence ≥ 3 sinyal\n"
        "• Hacim anomalisi (3x normal)\n"
        "• Destek/direnç yakınlaşması\n"
        f"• Her sabah {GUNLUK_RAPOR_SAATI} günlük rapor\n"
        f"• Her Cuma {HAFTALIK_OZET_SAAT} haftalık özet\n\n"
        "⚠️ <i>Yatırım tavsiyesi değildir.</i>"
    )


# ════════════════════════════════════════════════════════
# 🔁  OTOMATİK TARAMA
# ════════════════════════════════════════════════════════

def bildirim_gonder_sync(mesaj: str):
    async def _g():
        bot = Bot(token=TELEGRAM_TOKEN)
        await bot.send_message(chat_id=CHAT_ID, text=mesaj, parse_mode="HTML")
    asyncio.run(_g())


def sinyal_tara():
    logger.info("Sinyal taraması başladı...")

    # 1. Temel RSI/MACD sinyalleri
    for interval in ["1d", "4h", "1h", "15m"]:
        df = veri_cek(interval, interval_period(interval))
        if df is None: continue
        df = indiktorleri_hesapla(df)
        s  = sinyal_uret(df, interval)

        for key, flag, yon in [
            (f"rsi_asiri_satim_{interval}", s["rsi_asiri_satim"], "AL"),
            (f"macd_kesisim_al_{interval}", s["macd_kesisim_al"],  "AL"),
            (f"macd_kesisim_sat_{interval}",s["macd_kesisim_sat"], "SAT"),
        ]:
            if flag and not son_sinyaller.get(key, False):
                mesaj = sinyal_mesaji_olustur(s, interval)
                if mesaj:
                    bildirim_gonder_sync(mesaj)
                    haftalik_sinyal_gecmisi.append({"yon": yon, "zaman": interval, "tip": "RSI/MACD"})
            son_sinyaller[key] = flag

    # 2 yıllık günlük veri (hafıza + anomali + confluence)
    df2y = veri_cek("1d", "2y")
    if df2y is None:
        return
    df2y = indiktorleri_hesapla(df2y)
    guncel = _f(df2y["Close"].iloc[-1])

    # 2. Confluence skoru
    c = confluence_skoru_hesapla(df2y)
    if c["al_puan"] >= CONFLUENCE_ESIK and c["al_puan"] > c["sat_puan"]:
        if not son_sinyaller.get("confluence_al"):
            bildirim_gonder_sync(confluence_mesaji(c))
            haftalik_sinyal_gecmisi.append({"yon": "AL", "tip": "CONFLUENCE"})
        son_sinyaller["confluence_al"] = True
    else:
        son_sinyaller["confluence_al"] = False

    if c["sat_puan"] >= CONFLUENCE_ESIK and c["sat_puan"] > c["al_puan"]:
        if not son_sinyaller.get("confluence_sat"):
            bildirim_gonder_sync(confluence_mesaji(c))
            haftalik_sinyal_gecmisi.append({"yon": "SAT", "tip": "CONFLUENCE"})
        son_sinyaller["confluence_sat"] = True
    else:
        son_sinyaller["confluence_sat"] = False

    # 3. Hacim anomalisi
    a = anomali_kontrol(df2y)
    if a and not son_sinyaller.get("anomali"):
        bildirim_gonder_sync(anomali_mesaji(a))
        haftalik_sinyal_gecmisi.append({"yon": "—", "tip": "ANOMALİ"})
        logger.info("Hacim anomali bildirimi gönderildi")
    son_sinyaller["anomali"] = bool(a)

    # 4. Seviye yakınlık alarmı
    seviye_mesaj = seviye_yakinlik_alarmi(df2y, guncel)
    seviye_key   = f"seviye_{round(guncel, 0)}"
    if seviye_mesaj and not son_sinyaller.get(seviye_key):
        bildirim_gonder_sync(seviye_mesaj)
    son_sinyaller[seviye_key] = bool(seviye_mesaj)

    # 5. Fiyat alarmları
    if FIYAT_ALARM_YUKARI > 0:
        tetik = guncel >= FIYAT_ALARM_YUKARI
        if tetik and not son_sinyaller["fiyat_alarm_yukari"]:
            bildirim_gonder_sync(
                f"🔔 <b>FİYAT ALARMI ↑</b>\n📌 MAKTK: <b>{guncel:.2f} ₺</b>\n"
                f"Hedef <b>{FIYAT_ALARM_YUKARI:.2f} ₺</b> aşıldı!"
            )
        son_sinyaller["fiyat_alarm_yukari"] = tetik

    if FIYAT_ALARM_ASAGI > 0:
        tetik = guncel <= FIYAT_ALARM_ASAGI
        if tetik and not son_sinyaller["fiyat_alarm_asagi"]:
            bildirim_gonder_sync(
                f"🔔 <b>FİYAT ALARMI ↓</b>\n📌 MAKTK: <b>{guncel:.2f} ₺</b>\n"
                f"Fiyat <b>{FIYAT_ALARM_ASAGI:.2f} ₺</b> altına düştü!"
            )
        son_sinyaller["fiyat_alarm_asagi"] = tetik


def gunluk_rapor_gonder():
    bildirim_gonder_sync(gunluk_rapor_olustur())


def haftalik_ozet_gonder():
    bildirim_gonder_sync(haftalik_ozet_olustur())
    haftalik_sinyal_gecmisi.clear()   # Yeni haftaya sıfırla


def zamanlayici_baslat():
    schedule.every(KONTROL_SIKLIGI_DK).minutes.do(sinyal_tara)
    schedule.every().day.at(GUNLUK_RAPOR_SAATI).do(gunluk_rapor_gonder)
    getattr(schedule.every(), HAFTALIK_OZET_GUNU).at(HAFTALIK_OZET_SAAT).do(haftalik_ozet_gonder)
    logger.info(f"Zamanlayıcı aktif — {KONTROL_SIKLIGI_DK}dk tarama | "
                f"Sabah rapor {GUNLUK_RAPOR_SAATI} | Haftalık {HAFTALIK_OZET_SAAT}")
    while True:
        schedule.run_pending()
        time.sleep(30)


# ════════════════════════════════════════════════════════
# 🚀  BOTU BAŞLAT
# ════════════════════════════════════════════════════════

def main():
    if "BURAYA" in TELEGRAM_TOKEN or "BURAYA" in str(CHAT_ID):
        print("⚠️  LÜTFEN TELEGRAM_TOKEN ve CHAT_ID değerlerini doldur!")
        return

    logger.info("MAKTK Bot v2 başlatılıyor...")
    threading.Thread(target=sinyal_tara,       daemon=True).start()
    threading.Thread(target=zamanlayici_baslat, daemon=True).start()

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("analiz",     cmd_analiz))
    app.add_handler(CommandHandler("confluence", cmd_confluence))
    app.add_handler(CommandHandler("hafiza",     cmd_hafiza))
    app.add_handler(CommandHandler("pattern",    cmd_pattern))
    app.add_handler(CommandHandler("senaryo",    cmd_senaryo))
    app.add_handler(CommandHandler("rapor",      cmd_rapor))
    app.add_handler(CommandHandler("fiyat",      cmd_fiyat))
    app.add_handler(CommandHandler("alarm",      cmd_alarm))
    app.add_handler(CommandHandler("yardim",     cmd_yardim))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, mesaj_isle))

    logger.info("Bot aktif! Telegram'dan /start yaz.")
    app.run_polling()


if __name__ == "__main__":
    main()
