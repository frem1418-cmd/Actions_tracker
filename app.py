import streamlit as st
import yfinance as yf
import pandas as pd
import numpy as np
import requests
import io
import feedparser
from datetime import datetime, timedelta
from textblob import TextBlob
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import time
from bs4 import BeautifulSoup
import streamlit as st
from streamlit_gsheets import GSheetsConnection
from deep_translator import GoogleTranslator
from concurrent.futures import ThreadPoolExecutor
import urllib.parse
import logging
logging.getLogger("streamlit").setLevel(logging.ERROR)

conn = st.connection("gsheets", type=GSheetsConnection)

# =======================================================================
# FONCTIONS NEWS
# =======================================================================

@st.cache_data(ttl=900)
def get_quick_news(ticker):
    news_list = []
    t_clean = ticker.split('.')[0].strip().upper()

    def process_general_google(url, badge_icon, default_source="Info", limit=10):
        news_list = []
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        try:
            r = requests.get(url, headers=headers, timeout=5)
            if r.status_code == 200:
                feed = feedparser.parse(r.text)
                for e in feed.entries[:limit]:
                    parts = e.title.rsplit(' - ', 1)
                    clean_title = parts[0]
                    source_name = parts[1] if len(parts) > 1 else default_source
                    pol = TextBlob(clean_title).sentiment.polarity
                    sentiment_label = "Positif" if pol > 0.1 else "Négatif" if pol < -0.1 else "Neutre"
                    icon_sent = "🟢" if pol > 0.1 else "🔴" if pol < -0.1 else "⚪"
                    try:
                        dt_obj = datetime(*e.published_parsed[:6])
                    except:
                        dt_obj = datetime.now()
                    news_list.append({
                        'dt_obj': dt_obj,
                        'titre': clean_title,
                        'lien': e.link,
                        'source': source_name,
                        'badge': f"{icon_sent} {badge_icon}",
                        'sentiment': sentiment_label
                    })
        except:
            pass
        return news_list

    def fetch_google_fr(t_clean):
        url = f"https://news.google.com/rss/search?q={t_clean}+bourse&hl=fr&gl=FR&ceid=FR:fr"
        return process_general_google(url, "🇫🇷")

    def fetch_google_us(t_clean):
        url = f"https://news.google.com/rss/search?q={t_clean}+stock+news&hl=en-US&gl=US&ceid=US:en"
        return process_general_google(url, "🌐")

    def fetch_google_agencies(t_clean):
        url = f"https://news.google.com/rss/search?q={t_clean}+source:Bloomberg+OR+source:Reuters&hl=en-US"
        return process_general_google(url, badge_icon="💎")

    def fetch_google_wires(t_clean):
        url = f"https://news.google.com/rss/search?q={t_clean}+source:PR_Newswire+OR+source:Business_Wire&hl=en-US"
        return process_general_google(url, badge_icon="📄", limit=20)

    def fetch_benzinga_fixed(t_clean):
        url = "https://www.benzinga.com/markets/feed"
        return process_general_google(url, "⚡ Benzinga", default_source="Benzinga")

    def fetch_seeking(t_clean):
        url = f"https://seekingalpha.com/symbol/{t_clean}/feed"
        return process_general_google(url, badge_icon="[:orange[a]]", default_source="Seeking Alpha", limit=3)

    tasks = []
    if '.PA' in ticker.upper():
        tasks.append(fetch_google_fr)
    else:
        tasks.extend([
            fetch_google_us,
            fetch_google_agencies,
            fetch_google_wires,
            fetch_benzinga_fixed,
            fetch_seeking
        ])

    with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
        futures = [executor.submit(task, t_clean) for task in tasks]
        for future in futures:
            try:
                news_list.extend(future.result(timeout=10))
            except:
                continue

    news_list.sort(key=lambda x: x['dt_obj'], reverse=True)

    now = datetime.now()
    for item in news_list:
        dt = item['dt_obj']
        if dt.date() == now.date():
            item['date'] = dt.strftime('Auj. %H:%M')
        elif (now.date() - dt.date()).days == 1:
            item['date'] = dt.strftime('Hier %H:%M')
        else:
            item['date'] = dt.strftime('%d/%m %H:%M')
    return news_list


@st.cache_data(ttl=3600)
def safe_translate(text):
    if not text or len(text) < 5:
        return text
    try:
        return GoogleTranslator(source='auto', target='fr').translate(text)
    except:
        return text


@st.cache_data(ttl=3600)
def translate_batch(titles_list):
    if not titles_list:
        return []
    try:
        combined_text = " ||| ".join(titles_list)
        translated_text = GoogleTranslator(source='auto', target='fr').translate(combined_text)
        translated_list = [t.strip() for t in translated_text.split("|||")]
        if len(translated_list) != len(titles_list):
            return titles_list
        return translated_list
    except Exception as e:
        print(f"Erreur batch translation: {e}")
        return titles_list


@st.cache_data(ttl=900)
def get_bundle_news(liste_tickers, ticker_to_name):
    all_news_combined = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        future_to_ticker = {executor.submit(get_quick_news, t): t for t in liste_tickers}
        for future in future_to_ticker:
            ticker_parent = future_to_ticker[future]
            try:
                articles = future.result(timeout=10)
                if articles:
                    for a in articles:
                        a['ticker_parent'] = ticker_parent
                        a['nom_propre'] = ticker_to_name.get(ticker_parent, ticker_parent)
                        all_news_combined.append(a)
            except Exception:
                continue
    return all_news_combined


@st.cache_data(ttl=86400)
def get_action_name(ticker):
    try:
        return yf.Ticker(ticker).info.get('longName', ticker)
    except:
        return ticker


@st.fragment(run_every="5m")
def news_dashboard_module(liste_tickers):
    col1, col2 = st.columns([0.8, 0.2])
    with col1:
        st.subheader("🗞️ Flux d'actualités en direct")
    with col2:
        if st.button("🔄 Actualiser", key="ref_action"):
            get_quick_news.clear()
            st.rerun(scope="fragment")

    for t in liste_tickers:
        nom_action = get_action_name(t)
        with st.expander(f"🏢 **{nom_action}** ({t})", expanded=True):
            articles = get_quick_news(t)
            if articles:
                for a in articles:
                    st.markdown(f"{a['badge']} | **{a['date']}** | [{a['titre']}]({a['lien']})")
            else:
                st.caption(f"Aucune actualité récente pour {t}.")


@st.fragment(run_every="5m")
def actualite_module(liste_tickers):
    col_search, col_sent, col_trad, col_ref = st.columns([0.4, 0.2, 0.2, 0.2])
    with col_search:
        query = st.text_input(
            "🔍 Rechercher...",
            placeholder="Action, mot-clé...",
            label_visibility="collapsed",
            key="news_search_input").lower().strip()

    with col_trad:
        mode_global_fr = st.toggle("🇫🇷", help="Traduction des titres en français",
                                   value=st.session_state.get('mode_fr', True),
                                   key="mode_fr")

    with col_ref:
        if st.button("🔄", help="Actualiser le flux", key="refresh_news_btn"):
            get_quick_news.clear()
            st.rerun(scope="fragment")

    if 'nb_news_display' not in st.session_state:
        st.session_state.nb_news_display = 40

    with col_sent:
        filtre_sent = st.selectbox(
            "Filtrer par sentiment",
            options=["Tous", "Positifs 🟢", "Négatifs 🔴"],
            label_visibility="collapsed",
        )

    with st.spinner("Récupération des actualités..."):
        all_news = get_bundle_news(liste_tickers, ticker_to_name)

    all_news.sort(key=lambda x: x.get('dt_obj', datetime.now()), reverse=True)

    unique_news = []
    titres_vus = set()

    for n in all_news:
        fingerprint = n['titre'].lower().strip()
        sent_label = n.get('sentiment', 'Neutre')
        match_sent = True

        if "Positifs" in filtre_sent and sent_label != "Positif":
            match_sent = False
        elif "Négatifs" in filtre_sent and sent_label != "Négatif":
            match_sent = False

        if match_sent:
            if fingerprint not in titres_vus:
                source_brut = n.get('source', '').lower()
                nom_brut = n.get('nom_propre', '').lower()
                if not query or (query in fingerprint or
                                 query in n.get('ticker_parent', '').lower() or
                                 query in source_brut or
                                 query in nom_brut):
                    unique_news.append(n)
                    titres_vus.add(fingerprint)

    st.markdown("---")
    if unique_news:
        news_to_display = unique_news[:st.session_state.nb_news_display]

        if st.session_state.get('mode_fr', False):
            with st.spinner("Traduction des titres..."):
                titres_originaux = [n['titre'] for n in news_to_display]
                titres_traduits = translate_batch(titres_originaux)
                for i, n in enumerate(news_to_display):
                    if i < len(titres_traduits):
                        n['titre_affiche'] = titres_traduits[i]
        else:
            for n in news_to_display:
                n['titre_affiche'] = n['titre']

        for n in news_to_display:
            titre_final = n.get('titre_affiche', n['titre'])
            source = n.get('source', 'Info')
            nom_action = n.get('nom_propre', n.get('ticker_parent', 'Action'))
            st.markdown(
                f"{n['badge']} | {n['date']} | **{nom_action}** : "
                f"[{titre_final}]({n['lien']}) *({source})*"
            )
    else:
        st.info("Aucune actualité trouvée.")

    if len(unique_news) > st.session_state.nb_news_display:
        st.write("---")
        c1, c2, c3 = st.columns([1, 2, 1])
        with c2:
            if st.button(f"Afficher plus de news (+40) ➕", width="stretch"):
                st.session_state.nb_news_display += 40
                st.rerun()


# =======================================================================
# WATCHLISTS
# =======================================================================

@st.cache_data(ttl=3600)
def load_all_watchlists():
    try:
        conn = st.connection("gsheets", type=GSheetsConnection)
        df = conn.read(worksheet="Watchlists")
        return df
    except Exception:
        return None


@st.cache_data(ttl=600)
def get_tickers_from_watchlist(watchlist_name):
    df = load_all_watchlists()
    if df is not None:
        row = df[df['list_name'] == watchlist_name]
        if not row.empty:
            return row.iloc[0]['tickers']
    return ""


@st.cache_data(ttl=3600)
def get_column_config():
    return conn.read(worksheet="Choix_colonnes")


def update_tickers_callback():
    new_val = st.session_state["ticker_editor"].upper()
    save_watchlist_gsheets(sel_list, new_val)
    st.cache_data.clear()


# =======================================================================
# RÉFÉRENTIELS
# =======================================================================

SECTORS_FR = {
    "Basic Materials": "Matériaux de base",
    "Communication Services": "Services de communication",
    "Consumer Cyclical": "Consommation cyclique",
    "Consumer Defensive": "Consommation défensive",
    "Energy": "Énergie",
    "Financial Services": "Services financiers",
    "Healthcare": "Santé",
    "Industrials": "Industrie",
    "Real Estate": "Immobilier",
    "Technology": "Technologie",
    "Utilities": "Services publics",
    "Financial": "Finance",
    "Consumer Discretionary": "Consommation discrétionnaire"
}

RECO_FR = {
    "strong_buy": "Achat Fort 🚀", "buy": "Achat ✅", "hold": "Conserver ⚖️",
    "underperform": "Alléger ⚠️", "sell": "Vendre ❌", "none": "N/A"
}

EXPLICATIONS = {
    "Bénéfice Net": "Indique si l'entreprise est rentable. Un score positif (> 0) est indispensable pour la pérennité.",
    "Cash Flow Opé.": "Mesure l'argent réel généré par l'activité. Il doit être positif pour payer les factures et investir.",
    "Progression ROA": "Compare la rentabilité des actifs (Bénéfice/Actifs). Une hausse montre une meilleure efficacité de l'outil de travail.",
    "Qualité Gains": "Vérifie que le Cash Flow > Bénéfice Net. Le symbole Δ (Delta) représente l'écart entre les deux. Si Δ est positif, le profit est soutenu par du cash réel.",
    "Taille Actifs": "Mesure si l'entreprise se développe. Une augmentation des actifs indique généralement une croissance ou des investissements."
}


# =======================================================================
# FONCTIONS DE CALCUL
# =======================================================================

def search_ticker(query):
    try:
        if not query: return []
        url = f"https://query2.finance.yahoo.com/v1/finance/search?q={query}"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=5)
        data = response.json()
        results = []
        for res in data.get('quotes', []):
            if res.get('quoteType') == 'EQUITY':
                label = f"{res.get('symbol')} - {res.get('longname')} ({res.get('exchDisp')})"
                results.append({"label": label, "symbol": res.get('symbol')})
        return results
    except:
        return []


def clean_num(n):
    if isinstance(n, str): return n
    if n is None or pd.isna(n): return "0"
    abs_n = abs(n)
    if abs_n >= 1e12: return f"{n/1e12:.2f} Tn"
    if abs_n >= 1e9:  return f"{n/1e9:.2f} Md"
    if abs_n >= 1e6:  return f"{n/1e6:.2f} M"
    return "{:g}".format(float("{:.2f}".format(n)))


def get_progression_pct(current, previous):
    if previous is None or previous == 0 or pd.isna(previous): return None
    return ((current - previous) / abs(previous)) * 100


def calculate_piotroski_advanced(stock):
    try:
        income, balance, cash = stock.financials, stock.balance_sheet, stock.cashflow

        def get_val(df, labels, period=0):
            if df is None or df.empty: return None
            available = {k.lower(): k for k in df.index}
            for label in labels:
                if label.lower() in available:
                    idx = available[label.lower()]
                    if len(df.columns) > period:
                        v = df.loc[idx].iloc[period]
                        if not pd.isna(v): return v
            return None

        ni_keys    = ['Net Income', 'NetIncome', 'Net Income Common Stockholders']
        ocf_keys   = ['Operating Cash Flow', 'Total Cash From Operating Activities']
        asset_keys = ['Total Assets', 'TotalAssets']

        ni, ocf, assets       = get_val(income, ni_keys, 0), get_val(cash, ocf_keys, 0), get_val(balance, asset_keys, 0)
        ni_p, ocf_p, assets_p = get_val(income, ni_keys, 1), get_val(cash, ocf_keys, 1), get_val(balance, asset_keys, 1)

        if None in [ni, ocf, assets]: return "Incomplet", {}

        roa_n, roa_p = ni / assets, (ni_p / assets_p if assets_p else 0)
        q_n, q_p = ocf - ni, (ocf_p - ni_p if (ni_p is not None and ocf_p is not None) else None)

        checks = {
            "Bénéfice Net":    {"status": ni > 0,       "detail": f"{clean_num(ni)}",    "comparaison": f"N-1: {clean_num(ni_p)} ({get_progression_pct(ni, ni_p):+.1f}%)" if ni_p else "> 0"},
            "Cash Flow Opé.":  {"status": ocf > 0,      "detail": f"{clean_num(ocf)}",   "comparaison": f"N-1: {clean_num(ocf_p)} ({get_progression_pct(ocf, ocf_p):+.1f}%)" if ocf_p else "> 0"},
            "Progression ROA": {"status": roa_n > roa_p,"detail": f"{roa_n:.2%}",        "comparaison": f"N-1: {roa_p:.2%} ({get_progression_pct(roa_n, roa_p):+.1f}%)" if roa_p else "N/A"},
            "Qualité Gains":   {"status": ocf > ni,     "detail": f"Δ {clean_num(q_n)}", "comparaison": f"N-1: Δ {clean_num(q_p)} ({get_progression_pct(q_n, q_p):+.1f}%)" if q_p is not None else "OCF > NI"},
            "Taille Actifs":   {"status": assets > (assets_p or 0), "detail": f"{clean_num(assets)}", "comparaison": f"N-1: {clean_num(assets_p)} ({get_progression_pct(assets, assets_p):+.1f}%)" if assets_p else "N/A"}
        }
        return f"{sum(1 for c in checks.values() if c['status'])}/5", checks
    except:
        return "N/A", {}


@st.cache_data(ttl=3600)
def fetch_stock_data(ticker_str):
    yf.set_tz_cache_location("/tmp")
    try:
        s    = yf.Ticker(ticker_str.strip())
        info = s.info
        p    = info.get("currentPrice") or info.get("regularMarketPrice")
        if p is None: return None

        ef, pf = info.get("forwardEps", 0), info.get("forwardPE", 15)
        vb     = ef * pf
        tm     = info.get("targetMeanPrice", 0)
        sh     = info.get("sharesOutstanding", 1)

        fcf_raw = s.cashflow.loc["Free Cash Flow"].dropna().head(3).mean() if "Free Cash Flow" in s.cashflow.index else 0
        vf  = (fcf_raw / sh * 1.05) * pf if sh > 0 else 0
        mods = [v for v in [vb, vf, tm] if v > 0]
        avg  = sum(mods) / len(mods) if mods else 0
        p_s, p_d = calculate_piotroski_advanced(s)

        current_year = datetime.now().year
        hist = s.history(start=f"{current_year}-01-01")

        perf_1j = perf_1m = perf_ytd = 0
        if len(hist) >= 2:
            c_veille      = hist['Close'].iloc[-2]
            c_debut_annee = hist['Close'].iloc[0]
            perf_1j  = ((p - c_veille) / c_veille) * 100
            perf_ytd = ((p - c_debut_annee) / c_debut_annee) * 100
            if len(hist) >= 20:
                perf_1m = ((p - hist['Close'].iloc[-20]) / hist['Close'].iloc[-20]) * 100
            else:
                perf_1m = perf_ytd

        def calc_cagr(ticker_obj, years):
            try:
                h = ticker_obj.history(period=f"{years}y")
                if len(h) < 20: return None
                c_start = h['Close'].iloc[0]
                c_end   = h['Close'].iloc[-1]
                if c_start <= 0: return None
                return ((c_end / c_start) ** (1 / years) - 1) * 100
            except:
                return None

        cagr_3y = calc_cagr(s, 3)
        cagr_5y = calc_cagr(s, 5)

        def fmt_p(v):
            return f"{v:+.2f}% {'📈' if v > 0 else '📉'}"

        def fmt_pct(v):
            if v is None or pd.isna(v): return "N/A"
            return f"{v:.2f}%"

        curr_raw = info.get('currency', 'EUR')
        sym = "$" if curr_raw == "USD" else "£" if curr_raw == "GBP" else "€"

        div_date     = info.get("exDividendDate")
        div_date_str = datetime.fromtimestamp(div_date).strftime('%d/%m/%Y') if div_date else "N/A"

        div_pay_date = info.get("dividendDate")
        div_pay_date_str = datetime.fromtimestamp(div_pay_date).strftime('%d/%m/%Y') if div_pay_date else "N/A"

        def _prochaine_date_resultats(ticker_obj, infos):
            ts = infos.get("earningsTimestampStart") or infos.get("earningsTimestamp")
            if ts:
                try:
                    return datetime.fromtimestamp(ts).strftime('%d/%m/%Y')
                except Exception:
                    pass
            try:
                cal = ticker_obj.calendar
                if isinstance(cal, dict):
                    ed = cal.get("Earnings Date")
                    if isinstance(ed, (list, tuple)) and len(ed) > 0:
                        return pd.to_datetime(ed[0]).strftime('%d/%m/%Y')
                    elif ed:
                        return pd.to_datetime(ed).strftime('%d/%m/%Y')
                elif hasattr(cal, "loc") and "Earnings Date" in getattr(cal, "index", []):
                    val = cal.loc["Earnings Date"]
                    val = val.iloc[0] if hasattr(val, "iloc") else val
                    return pd.to_datetime(val).strftime('%d/%m/%Y')
            except Exception:
                pass
            return "N/A"

        prochains_resultats_str = _prochaine_date_resultats(s, info)

        trailing_eps = info.get("trailingEps", 0) or 0
        trailing_pe  = info.get("trailingPE",  0) or 0

        eps_growth = info.get("earningsGrowth")
        if eps_growth and eps_growth != 0 and trailing_pe:
            peg_actuel = trailing_pe / (eps_growth * 100)
        else:
            peg_actuel = None

        fwd_eps_growth = info.get("earningsQuarterlyGrowth") or info.get("revenueGrowth")
        if fwd_eps_growth and fwd_eps_growth != 0 and pf:
            peg_forward = pf / (fwd_eps_growth * 100)
        else:
            peg_forward = None

        roa         = info.get("returnOnAssets")
        roe         = info.get("returnOnEquity")
        marge_nette = info.get("profitMargins")
        dette_equity = info.get("debtToEquity")
        beta        = info.get("beta")

        def pct_fmt(v):
            if v is None or (isinstance(v, float) and pd.isna(v)): return "N/A"
            return f"{v*100:.1f}%"

        def num_fmt(v, decimals=2):
            if v is None or (isinstance(v, float) and pd.isna(v)): return "N/A"
            return f"{v:.{decimals}f}"

        # --- Croissance EBITDA ---
        try:
            ebitda_curr = s.financials.loc['EBITDA'].iloc[0] if 'EBITDA' in s.financials.index else None
            ebitda_prev = s.financials.loc['EBITDA'].iloc[1] if ('EBITDA' in s.financials.index and len(s.financials.columns) > 1) else None
            if (ebitda_curr is not None and ebitda_prev is not None
                    and ebitda_prev != 0
                    and not pd.isna(ebitda_curr) and not pd.isna(ebitda_prev)):
                ebitda_growth = ((ebitda_curr - ebitda_prev) / abs(ebitda_prev)) * 100
            else:
                ebitda_growth = None
        except Exception:
            ebitda_growth = None

        # --- P/FCF (actuel) ---
        try:
            fcf_total = s.cashflow.loc['Free Cash Flow'].dropna().iloc[0] if 'Free Cash Flow' in s.cashflow.index else None
            if fcf_total and sh and sh > 0 and not pd.isna(fcf_total):
                fcf_per_share = fcf_total / sh
                p_fcf = p / fcf_per_share if fcf_per_share > 0 else None
            else:
                p_fcf = None
        except Exception:
            p_fcf = None

        # --- P/FCF historique moyen (3 dernières années) ---
        try:
            fcf_series = s.cashflow.loc['Free Cash Flow'].dropna() if 'Free Cash Flow' in s.cashflow.index else None
            if fcf_series is not None and sh and sh > 0 and len(fcf_series) >= 2:
                hist_pfcf_vals = []
                for fcf_yr in fcf_series.head(3):
                    if not pd.isna(fcf_yr) and fcf_yr > 0:
                        fcf_ps_yr = fcf_yr / sh
                        hist_pfcf_vals.append(p / fcf_ps_yr)
                p_fcf_moy = sum(hist_pfcf_vals) / len(hist_pfcf_vals) if hist_pfcf_vals else None
            else:
                p_fcf_moy = None
        except Exception:
            p_fcf_moy = None

        def fmt_growth(v):
            if v is None or (isinstance(v, float) and pd.isna(v)): return "N/A"
            return f"{v:+.1f}%"

        def fmt_ratio(v, decimals=1):
            if v is None or (isinstance(v, float) and pd.isna(v)): return "N/A"
            return f"{v:.{decimals}f}x"

        return {
            "Ticker": ticker_str,
            "Nom": info.get("longName", ticker_str),
            "Secteur": SECTORS_FR.get(info.get("sector"), info.get("sector")),
            "Prix Actuel": p,
            "BNA Actuel": trailing_eps,
            "PER Actuel": trailing_pe,
            "PEG Actuel": round(peg_actuel, 2) if peg_actuel else None,
            "PEG Forward": round(peg_forward, 2) if peg_forward else None,
            "ROA": pct_fmt(roa),
            "ROE": pct_fmt(roe),
            "Marge Nette": pct_fmt(marge_nette),
            "Dette/Equity": num_fmt(dette_equity) if dette_equity else "N/A",
            "Beta": num_fmt(beta) if beta else "N/A",
            "Croissance EBITDA": fmt_growth(ebitda_growth),
            "P/FCF": fmt_ratio(p_fcf),
            "P/FCF Moy 3a": fmt_ratio(p_fcf_moy),   # NOUVEAU
            "CAGR 3 ans": fmt_pct(cagr_3y),
            "CAGR 5 ans": fmt_pct(cagr_5y),
            "Chg 1J": fmt_p(perf_1j),
            "Chg YTD": fmt_p(perf_ytd),
            "Chg 1M": fmt_p(perf_1m),
            "currency": sym,
            "BNA Forward": ef,
            "PER Forward": pf,
            "Nb Analystes": info.get("numberOfAnalystOpinions", 0),
            "Entrée BNA -15%": vb * 0.85,
            "Entrée FCF -15%": vf * 0.85,
            "Entrée Analystes -15%": tm * 0.85,
            "Entrée Synthèse (-15%)": avg * 0.85,
            "Santé (Piotroski)": p_s,
            "Dividende (€/$)": info.get("dividendRate", 0),
            "Rendement %": round((info.get("dividendRate", 0) / p * 100), 2) if info.get("dividendRate") else 0,
            "Date Détachement": div_date_str,
            "Date Versement Dividende": div_pay_date_str,
            "Prochains Résultats": prochains_resultats_str,
            "Avis Analystes": RECO_FR.get(info.get("recommendationKey"), "N/A"),
            # Colonnes internes (non affichées dans le tableau)
            "p_details": p_d,
            "full_data": {
                "val_bna": vb, "val_fcf": vf, "target_mean": tm, "fair_avg": avg,
                "currency": info.get("currency", "EUR"),
                "eps_fwd": ef, "per_fwd": pf,
                "fcf_ps": fcf_raw / sh if sh > 0 else 0,
                "num_analysts": info.get("numberOfAnalystOpinions", 0)
            }
        }
    except:
        return None


def afficher_detail_action(d):
    """Fiche détaillée d'une action (santé financière, graphique, valorisation, actualités).
    Réutilisée depuis la vue Portefeuille et depuis la vue Indices."""
    fd = d['full_data']
    st.divider()

    c1, c2 = st.columns([2, 1])

    with c1:
        st.header(f"🏢 {d['Nom']} ({d['Ticker']})")
        st.subheader("🏥 Diagnostic Santé Financière")
        grid = st.columns(5)
        for i, (label, info) in enumerate(d['p_details'].items()):
            with grid[i]:
                txt_c = info.get('comparaison', '')
                col_v = "#28a745" if "+" in txt_c else ("#dc3545" if "-" in txt_c else "#555")
                st.markdown(f"""
                <div title="{EXPLICATIONS.get(label, '')}" style='background:#f8f9fa; padding:10px; border-radius:10px; text-align:center; border:1px solid #ddd; height:180px; cursor:help; display:flex; flex-direction:column; justify-content:center;'>
                    <div style='font-weight:bold; color:#555; font-size:0.8em; margin-bottom:5px;'>{label} ℹ️</div>
                    <div style='font-size:1em; font-weight:bold;'>{info.get('detail', 'N/A')}</div>
                    <div style='font-size:0.75em; color:{col_v}; font-weight:bold; background:white; padding:3px; border-radius:4px; border:1px solid #eee; margin: 5px 0;'>{txt_c}</div>
                    <div style='font-size:1.4em;'>{'✅' if info.get('status') else '❌'}</div>
                </div>
                """, unsafe_allow_html=True)

        # ===================================================
        # GRAPHIQUE
        # ===================================================
        st.divider()
        st.subheader(f"📈 Performance & Volumes")

        try:
            s_obj      = yf.Ticker(d['Ticker'])
            current_yr = datetime.now().year

            # Contrôles ligne 1 : Période + Indicateur
            ctrl_col1, ctrl_col2, ctrl_col3 = st.columns([0.30, 0.42, 0.28])
            with ctrl_col1:
                st.caption("📅 Période")
                periode_choisie = st.radio(
                    "Période",
                    ["YTD", "1 an", "2 ans", "5 ans", "Max"],
                    horizontal=True,
                    key=f"periode_{d['Ticker']}",
                    label_visibility="collapsed"
                )
            with ctrl_col2:
                st.caption("📊 Indicateur (panneau central)")
                indicateur_choisi = st.radio(
                    "Indicateur",
                    ["RSI", "MACD", "Bollinger + MA"],
                    index=0,
                    horizontal=True,
                    key=f"indic_{d['Ticker']}",
                    label_visibility="collapsed"
                )
            with ctrl_col3:
                st.caption("📉 Graphique bas")
                graph_bas = st.radio(
                    "Graphique bas",
                    ["PER historique", "P/FCF historique", "Les deux", "Aucun"],
                    index=0,
                    horizontal=False,
                    key=f"graph_bas_{d['Ticker']}",
                    label_visibility="collapsed"
                )

            # Dates
            today  = datetime.now()
            warmup = 250
            if periode_choisie == "YTD":
                date_calcul    = (datetime(current_yr, 1, 1) - timedelta(days=warmup)).strftime('%Y-%m-%d')
                date_affichage = f"{current_yr}-01-01"
            elif periode_choisie == "1 an":
                date_calcul    = (today - timedelta(days=365 + warmup)).strftime('%Y-%m-%d')
                date_affichage = (today - timedelta(days=365)).strftime('%Y-%m-%d')
            elif periode_choisie == "2 ans":
                date_calcul    = (today - timedelta(days=730 + warmup)).strftime('%Y-%m-%d')
                date_affichage = (today - timedelta(days=730)).strftime('%Y-%m-%d')
            elif periode_choisie == "5 ans":
                date_calcul    = (today - timedelta(days=1825 + warmup)).strftime('%Y-%m-%d')
                date_affichage = (today - timedelta(days=1825)).strftime('%Y-%m-%d')
            else:
                date_calcul    = "1985-01-01"
                date_affichage = "1985-01-01"

            h_data_large = s_obj.history(start=date_calcul)

            # --- Historique P/FCF annuel (calculé une fois, avant les subplots) ---
            pfcf_hist_data = {}
            try:
                sh_val = s_obj.info.get("sharesOutstanding", 1)
                cf_df  = s_obj.cashflow
                if 'Free Cash Flow' in cf_df.index and sh_val and sh_val > 0:
                    fcf_row = cf_df.loc['Free Cash Flow'].dropna()
                    for col_date, fcf_val in fcf_row.items():
                        if not pd.isna(fcf_val) and fcf_val > 0:
                            fcf_ps_yr = fcf_val / sh_val
                            yr = col_date.year
                            mask_yr = h_data_large.index.year == yr
                            prix_yr = h_data_large.loc[mask_yr, 'Close'].mean() if mask_yr.sum() > 0 else None
                            if prix_yr is not None and not pd.isna(prix_yr) and prix_yr > 0:
                                pfcf_hist_data[col_date] = prix_yr / fcf_ps_yr
            except Exception:
                pfcf_hist_data = {}

            if not h_data_large.empty:

                # Calcul indicateurs sur historique complet (avec warmup)
                h_data_large['MA20']  = h_data_large['Close'].rolling(20).mean()
                h_data_large['MA50']  = h_data_large['Close'].rolling(50).mean()
                h_data_large['MA100'] = h_data_large['Close'].rolling(100).mean()
                h_data_large['MA200'] = h_data_large['Close'].rolling(200).mean()

                h_data_large['BB_std']   = h_data_large['Close'].rolling(20).std()
                h_data_large['BB_upper'] = h_data_large['MA20'] + h_data_large['BB_std'] * 2
                h_data_large['BB_lower'] = h_data_large['MA20'] - h_data_large['BB_std'] * 2

                delta_c = h_data_large['Close'].diff()
                gain_c  = delta_c.clip(lower=0).rolling(14).mean()
                loss_c  = (-delta_c.clip(upper=0)).rolling(14).mean()
                rs_c    = gain_c / loss_c.replace(0, float('nan'))
                h_data_large['RSI'] = 100 - (100 / (1 + rs_c))

                ema12 = h_data_large['Close'].ewm(span=12, adjust=False).mean()
                ema26 = h_data_large['Close'].ewm(span=26, adjust=False).mean()
                h_data_large['MACD']        = ema12 - ema26
                h_data_large['MACD_signal'] = h_data_large['MACD'].ewm(span=9, adjust=False).mean()
                h_data_large['MACD_hist']   = h_data_large['MACD'] - h_data_large['MACD_signal']

                bna_actuel = d.get('BNA Actuel', 0)
                if bna_actuel and bna_actuel > 0:
                    h_data_large['PER_hist'] = h_data_large['Close'] / bna_actuel

                # Filtre période d'affichage
                h_data = h_data_large[h_data_large.index >= date_affichage].copy()

                colors_vol = [
                    '#28a745' if row['Close'] >= row['Open'] else '#dc3545'
                    for _, row in h_data.iterrows()
                ]

                # ---- SUBPLOTS ----
                fig = make_subplots(
                    rows=3, cols=1,
                    shared_xaxes=True,
                    row_heights=[0.55, 0.25, 0.20],
                    vertical_spacing=0.03,
                    subplot_titles=[None, None, None],
                    specs=[
                        [{"secondary_y": True}],
                        [{"secondary_y": False}],
                        [{"secondary_y": False}],
                    ]
                )

                # Row 1 : Volume (axe gauche)
                fig.add_trace(go.Bar(
                    x=h_data.index, y=h_data['Volume'],
                    name="Volume", marker_color=colors_vol, opacity=0.30
                ), row=1, col=1, secondary_y=False)

                # Row 1 : Prix (axe droit)
                fig.add_trace(go.Scatter(
                    x=h_data.index, y=h_data['Close'],
                    name="Prix", line=dict(color='#1a73e8', width=2)
                ), row=1, col=1, secondary_y=True)

                # MA50 toujours visible
                fig.add_trace(go.Scatter(
                    x=h_data.index, y=h_data['MA50'],
                    name="MA50", line=dict(color='orange', dash='dot', width=1.5)
                ), row=1, col=1, secondary_y=True)

                # MA100
                if h_data['MA100'].notna().sum() > 10:
                    fig.add_trace(go.Scatter(
                        x=h_data.index, y=h_data['MA100'],
                        name="MA100", line=dict(color='#00bcd4', dash='dot', width=1.5)
                    ), row=1, col=1, secondary_y=True)

                # MA200
                if h_data['MA200'].notna().sum() > 10:
                    fig.add_trace(go.Scatter(
                        x=h_data.index, y=h_data['MA200'],
                        name="MA200", line=dict(color='#e91e63', dash='dot', width=1.5)
                    ), row=1, col=1, secondary_y=True)

                # Bollinger sur le cours si mode sélectionné
                if indicateur_choisi == "Bollinger + MA":
                    fig.add_trace(go.Scatter(
                        x=h_data.index, y=h_data['BB_upper'],
                        name="BB Sup", line=dict(color='rgba(100,100,200,0.5)', width=1), fill=None
                    ), row=1, col=1, secondary_y=True)
                    fig.add_trace(go.Scatter(
                        x=h_data.index, y=h_data['BB_lower'],
                        name="BB Inf", line=dict(color='rgba(100,100,200,0.5)', width=1),
                        fill='tonexty', fillcolor='rgba(100,100,200,0.07)'
                    ), row=1, col=1, secondary_y=True)
                    fig.add_trace(go.Scatter(
                        x=h_data.index, y=h_data['MA20'],
                        name="MA20", line=dict(color='purple', dash='dot', width=1)
                    ), row=1, col=1, secondary_y=True)

                # Lignes prix actuel & zone achat
                prix_actuel = d['Prix Actuel']
                fig.add_shape(
                    type="line", xref="paper", x0=0, x1=1,
                    yref="y2", y0=prix_actuel, y1=prix_actuel,
                    line=dict(color="gray", dash="dash", width=1.5)
                )
                fig.add_annotation(
                    xref="paper", x=1.01, yref="y2",
                    y=prix_actuel, text=f"  {prix_actuel:.2f}",
                    showarrow=False, font=dict(color="gray", size=11)
                )
                fig.add_shape(
                    type="line", xref="paper", x0=0, x1=1,
                    yref="y2", y0=prix_actuel * 0.85, y1=prix_actuel * 0.85,
                    line=dict(color="#28a745", dash="dot", width=1.5)
                )
                fig.add_annotation(
                    xref="paper", x=0, yref="y2",
                    y=prix_actuel * 0.85, text="Zone achat (-15%)  ",
                    showarrow=False, xanchor="right",
                    font=dict(color="#28a745", size=10)
                )

                # ---- Row 2 : Indicateur ----
                if indicateur_choisi == "RSI":
                    fig.add_trace(go.Scatter(
                        x=h_data.index, y=h_data['RSI'],
                        name="RSI(14)", line=dict(color='#9c27b0', width=2)
                    ), row=2, col=1)
                    fig.add_hrect(y0=70, y1=100, fillcolor="rgba(220,53,69,0.08)",  line_width=0, row=2, col=1)
                    fig.add_hrect(y0=0,  y1=30,  fillcolor="rgba(40,167,69,0.08)",  line_width=0, row=2, col=1)
                    fig.add_hline(y=70, line_color="rgba(220,53,69,0.5)", line_dash="dot",
                                  annotation_text="Surachat 70", annotation_position="right", row=2, col=1)
                    fig.add_hline(y=30, line_color="rgba(40,167,69,0.5)", line_dash="dot",
                                  annotation_text="Survente 30", annotation_position="right", row=2, col=1)
                    fig.update_yaxes(range=[0, 100], title_text="RSI", row=2, col=1)

                elif indicateur_choisi == "MACD":
                    macd_colors = ['#28a745' if v >= 0 else '#dc3545' for v in h_data['MACD_hist'].fillna(0)]
                    fig.add_trace(go.Bar(
                        x=h_data.index, y=h_data['MACD_hist'],
                        name="Histogramme", marker_color=macd_colors, opacity=0.6
                    ), row=2, col=1)
                    fig.add_trace(go.Scatter(
                        x=h_data.index, y=h_data['MACD'],
                        name="MACD", line=dict(color='#1a73e8', width=1.5)
                    ), row=2, col=1)
                    fig.add_trace(go.Scatter(
                        x=h_data.index, y=h_data['MACD_signal'],
                        name="Signal", line=dict(color='orange', width=1.5)
                    ), row=2, col=1)
                    fig.add_hline(y=0, line_color="gray", line_dash="dot", row=2, col=1)
                    fig.update_yaxes(title_text="MACD", row=2, col=1)

                elif indicateur_choisi == "Bollinger + MA":
                    bb_range = h_data['BB_upper'] - h_data['BB_lower']
                    h_data['BB_pct'] = (
                        (h_data['Close'] - h_data['BB_lower'])
                        / bb_range.replace(0, float('nan'))
                    ) * 100
                    fig.add_trace(go.Scatter(
                        x=h_data.index, y=h_data['BB_pct'],
                        name="%B Bollinger", line=dict(color='purple', width=2)
                    ), row=2, col=1)
                    fig.add_hrect(y0=80,  y1=130, fillcolor="rgba(220,53,69,0.08)",  line_width=0, row=2, col=1)
                    fig.add_hrect(y0=-30, y1=20,  fillcolor="rgba(40,167,69,0.08)",  line_width=0, row=2, col=1)
                    fig.add_hline(y=80, line_color="rgba(220,53,69,0.4)", line_dash="dot",
                                  annotation_text="Haut BB", annotation_position="right", row=2, col=1)
                    fig.add_hline(y=20, line_color="rgba(40,167,69,0.4)", line_dash="dot",
                                  annotation_text="Bas BB", annotation_position="right", row=2, col=1)
                    fig.update_yaxes(title_text="%B", row=2, col=1)

                # ---- Row 3 : Graphique bas configurable ----
                afficher_per  = graph_bas in ["PER historique", "Les deux"]
                afficher_pfcf = graph_bas in ["P/FCF historique", "Les deux"]

                if afficher_per:
                    if 'PER_hist' in h_data.columns and h_data['PER_hist'].notna().sum() > 5:
                        fig.add_trace(go.Scatter(
                            x=h_data.index, y=h_data['PER_hist'],
                            name="PER historique",
                            line=dict(color='#ff9800', width=1.5),
                            fill='tozeroy', fillcolor='rgba(255,152,0,0.08)'
                        ), row=3, col=1)
                        for per_ref, per_col, per_lbl in [
                            (15, "rgba(40,167,69,0.6)",   "PER 15"),
                            (20, "rgba(100,100,200,0.5)", "PER 20"),
                            (30, "rgba(220,53,69,0.6)",   "PER 30"),
                        ]:
                            fig.add_hline(
                                y=per_ref, line_color=per_col, line_dash="dot",
                                annotation_text=per_lbl, annotation_position="right",
                                row=3, col=1
                            )
                        lbl_axe_per = "PER / P/FCF" if afficher_pfcf else "PER"
                        fig.update_yaxes(title_text=lbl_axe_per, row=3, col=1)
                    else:
                        fig.add_annotation(
                            xref="paper", yref="paper", x=0.5, y=0.04,
                            text="PER non disponible (BNA = 0 ou négatif)",
                            showarrow=False, font=dict(color="gray", size=10)
                        )

                if afficher_pfcf:
                    if pfcf_hist_data:
                        pfcf_dates = sorted(pfcf_hist_data.keys())
                        pfcf_vals  = [pfcf_hist_data[d_key] for d_key in pfcf_dates]
                        fig.add_trace(go.Scatter(
                            x=pfcf_dates, y=pfcf_vals,
                            name="P/FCF historique",
                            mode="lines+markers",
                            line=dict(color='#00bcd4', width=2),
                            marker=dict(size=8),
                            fill='tozeroy', fillcolor='rgba(0,188,212,0.08)'
                        ), row=3, col=1)
                        # P/FCF actuel comme ligne de référence
                        try:
                            pfcf_actuel_val = float(str(d.get('P/FCF', '0')).replace('x', ''))
                            if pfcf_actuel_val > 0:
                                fig.add_hline(
                                    y=pfcf_actuel_val,
                                    line_color="rgba(0,188,212,0.8)", line_dash="dash",
                                    annotation_text=f"Actuel {pfcf_actuel_val:.1f}x",
                                    annotation_position="right", row=3, col=1
                                )
                        except Exception:
                            pass
                        # P/FCF moyen 3 ans comme ligne de référence
                        try:
                            pfcf_moy_val = float(str(d.get('P/FCF Moy 3a', '0')).replace('x', ''))
                            if pfcf_moy_val > 0:
                                fig.add_hline(
                                    y=pfcf_moy_val,
                                    line_color="rgba(0,188,212,0.4)", line_dash="dot",
                                    annotation_text=f"Moy 3a {pfcf_moy_val:.1f}x",
                                    annotation_position="left", row=3, col=1
                                )
                        except Exception:
                            pass
                        for pfcf_ref, pfcf_col, pfcf_lbl in [
                            (15, "rgba(40,167,69,0.6)",  "P/FCF 15x"),
                            (25, "rgba(220,53,69,0.6)",  "P/FCF 25x"),
                        ]:
                            fig.add_hline(
                                y=pfcf_ref, line_color=pfcf_col, line_dash="dot",
                                annotation_text=pfcf_lbl, annotation_position="left",
                                row=3, col=1
                            )
                        lbl_axe = "PER / P/FCF" if afficher_per else "P/FCF"
                        fig.update_yaxes(title_text=lbl_axe, row=3, col=1)
                    else:
                        if not afficher_per:
                            fig.add_annotation(
                                xref="paper", yref="paper", x=0.5, y=0.04,
                                text="P/FCF historique non disponible",
                                showarrow=False, font=dict(color="gray", size=10)
                            )

                if graph_bas == "Aucun":
                    fig.update_yaxes(visible=False, row=3, col=1)
                    fig.update_xaxes(visible=False, row=3, col=1)

                # ---- Layout global ----
                fig.update_layout(
                    title=dict(
                        text=f"<b>{d['Nom']}</b> ({d['Ticker']})",
                        font=dict(size=13, color="#555"),
                        x=0,
                        pad=dict(b=0)
                    ),
                    height=700,
                    margin=dict(l=10, r=70, t=35, b=10),
                    hovermode="x unified",
                    template="plotly_white",
                    legend=dict(
                        orientation="h",
                        yanchor="top", y=-0.05,
                        xanchor="center", x=0.5,
                        font=dict(size=11)
                    )
                )

                # Axe gauche row 1 = Volume
                fig.update_yaxes(
                    title_text="Volume",
                    secondary_y=False, row=1, col=1,
                    showgrid=False, fixedrange=False,
                    tickformat=".2s", side="left"
                )
                # Axe droit row 1 = Prix
                fig.update_yaxes(
                    title_text="Prix",
                    secondary_y=True, row=1, col=1,
                    showgrid=True, gridcolor='rgba(200,200,200,0.4)',
                    fixedrange=False, side="right"
                )
                fig.update_xaxes(showgrid=False)

                st.plotly_chart(
                    fig,
                    use_container_width=True,
                    config={
                        'scrollZoom': True,
                        'displayModeBar': True,
                        'editable': True,
                        'modeBarButtonsToAdd': ['drawline', 'drawrect', 'eraseshape'],
                        'displaylogo': False
                    }
                )
            else:
                st.info("Données historiques non disponibles.")

        except Exception as e:
            st.error(f"Erreur lors du chargement du graphique : {e}")

        st.divider()
        st.subheader("🏆 Modèles de Valorisation")
        v_configs = [
            ("1️⃣ Modèle BNA (Forward)", fd['val_bna'], f"BNA Fwd ({clean_num(fd['eps_fwd'])}) × PER Fwd ({fd['per_fwd']})"),
            ("2️⃣ Modèle FCF (Moyen)",   fd['val_fcf'], f"(FCF/Action {clean_num(fd['fcf_ps'])}) × 1.05 × PER Fwd"),
            ("3️⃣ Analystes",             fd['target_mean'], f"Moyenne de {fd['num_analysts']} opinions")
        ]
        for title, val, formula in v_configs:
            if val > 0:
                with st.expander(f"{title} : {clean_num(val)} {fd['currency']}", expanded=True):
                    st.caption(f"Calcul : {formula}")
                    m1, m2, m3, m4 = st.columns(4)
                    m1.metric("Juste Prix", clean_num(val))
                    m2.metric("-10%", clean_num(val * 0.9))
                    m3.metric("-12%", clean_num(val * 0.88))
                    m4.metric("-15%", clean_num(val * 0.85))

    with c2:
        st.metric("Prix Actuel", f"{clean_num(d['Prix Actuel'])} {fd['currency']}")

        # Affichage P/FCF actuel et moyen côte à côte
        pfcf_col1, pfcf_col2 = st.columns(2)
        with pfcf_col1:
            pfcf_val_str = d.get('P/FCF', 'N/A')
            try:
                pfcf_num = float(str(pfcf_val_str).replace('x', ''))
                pfcf_color = "#28a745" if pfcf_num < 15 else ("#dc3545" if pfcf_num > 30 else "#ff9800")
            except:
                pfcf_color = "#555"
            st.markdown(
                f"<div style='background:#f8f9fa; border:1px solid #ddd; border-radius:8px; padding:10px; text-align:center;'>"
                f"<div style='font-size:0.75em; color:#555; font-weight:bold;'>P/FCF Actuel</div>"
                f"<div style='font-size:1.3em; font-weight:bold; color:{pfcf_color};'>{pfcf_val_str}</div>"
                f"</div>",
                unsafe_allow_html=True
            )
        with pfcf_col2:
            pfcf_moy_str = d.get('P/FCF Moy 3a', 'N/A')
            try:
                pfcf_moy_num = float(str(pfcf_moy_str).replace('x', ''))
                pfcf_moy_color = "#28a745" if pfcf_moy_num < 15 else ("#dc3545" if pfcf_moy_num > 30 else "#ff9800")
            except:
                pfcf_moy_color = "#555"
            st.markdown(
f"<div style='background:#f8f9fa; border:1px solid #ddd; border-radius:8px; padding:10px; text-align:center;'>"
                f"<div style='font-size:0.75em; color:#555; font-weight:bold;'>P/FCF Moy 3a</div>"
                f"<div style='font-size:1.3em; font-weight:bold; color:{pfcf_moy_color};'>{pfcf_moy_str}</div>"
                f"</div>",
                unsafe_allow_html=True
            )

        st.markdown("<br>", unsafe_allow_html=True)
        st.markdown(
            f"<div style='background:#28a745; color:white; padding:25px; border-radius:15px; text-align:center;'>"
            f"<small>ENTRÉE CONSEILLÉE (-15%)</small><br/>"
            f"<span style='font-size:36px; font-weight:bold;'>{clean_num(fd['fair_avg']*0.85)}</span></div>",
            unsafe_allow_html=True
        )
        st.divider()
        st.write(f"**Dividende :** {clean_num(d['Dividende (€/$)'])} {fd['currency']} ({d['Rendement %']}%)")
        st.write(f"**Détachement :** {d['Date Détachement']} | **Versement :** {d.get('Date Versement Dividende', 'N/A')}")
        st.write(f"**Prochains résultats :** {d.get('Prochains Résultats', 'N/A')}")
        st.write(f"**Avis :** {d['Avis Analystes']} | **Secteur :** {d['Secteur']}")

        ticker_clean   = str(d['Ticker']).split('.')[0].upper() if d and 'Ticker' in d else "AAPL"
        nom_action_vue = d.get('Nom', ticker_clean)

        st.divider()
        col_titre, col_switch = st.columns([3, 1])
        with col_titre:
            st.markdown(f"### 📰 Dernières Actualités : {nom_action_vue}")
        with col_switch:
            mode_fr = st.toggle("FR", help="Traduction automatique des titres en français", value=False)

        all_news = get_quick_news(ticker_clean)

        unique_news = []
        titres_vus  = set()
        if all_news:
            all_news.sort(key=lambda x: x.get('dt_obj', datetime.now()), reverse=True)
            for article in all_news:
                t_brut = article.get('titre', '').lower().strip()
                if t_brut not in titres_vus:
                    unique_news.append(article)
                    titres_vus.add(t_brut)

        query = st.session_state.get("main_search", "")
        if query:
            q = query.lower()
            unique_news = [
                a for a in unique_news
                if q in a.get('titre', '').lower()
                or q in a.get('source', '').lower()
                or q in a.get('ticker_parent', '').lower()
            ]

        for article in unique_news[:20]:
            lien_reel  = article.get('lien', '#')
            source     = article.get('source', 'Info').strip('() ')
            date       = article.get('date', 'Auj.')
            badge      = article.get('badge', '🌐')
            titre_brut = article.get('titre', 'Sans titre')
            is_seeking = "seekingalpha.com" in lien_reel.lower()
            mots_en    = {'the', 'stock', 'growth', 'fed', 'market', 'earnings'}
            est_anglais = any(w in titre_brut.lower() for w in mots_en) or "seekingalpha" in lien_reel.lower()

            titre_affiche = safe_translate(titre_brut) if (mode_fr and est_anglais) else titre_brut
            label = f"{badge} | **{date}** | {titre_affiche}"

            with st.expander(label):
                st.write(f"**Origine :** {source}")
                if is_seeking or not est_anglais:
                    st.link_button("📖 Lire l'article complet", lien_reel, width="stretch")
                else:
                    c1, c2 = st.columns(2)
                    with c1:
                        st.link_button("📄 Original (EN)", lien_reel, width="stretch")
                    with c2:
                        lien_propre = urllib.parse.quote(lien_reel, safe='')
                        url_t = f"https://translate.google.com/translate?sl=auto&tl=fr&u={lien_propre}"
                        st.link_button("🇫🇷 Traduire Page", url_t, type="primary", width="stretch")
                if mode_fr and est_anglais:
                    st.caption(f"Original : {titre_brut}")


# =======================================================================
# INDICES PRINCIPAUX
# =======================================================================

INDICES_PRINCIPAUX = {
    "S&P 500": {
        "symbole_yf": "^GSPC",
        "wiki_url": "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
        "col_ticker_candidates": ["Symbol"],
        "col_nom_candidates": ["Security"],
        "suffixe_yf": "",
    },
    "Nasdaq 100": {
        "symbole_yf": "^NDX",
        "wiki_url": "https://en.wikipedia.org/wiki/Nasdaq-100",
        "col_ticker_candidates": ["Ticker", "Symbol"],
        "col_nom_candidates": ["Company", "Security"],
        "suffixe_yf": "",
    },
    "CAC 40": {
        "symbole_yf": "^FCHI",
        "wiki_url": "https://en.wikipedia.org/wiki/CAC_40",
        "col_ticker_candidates": ["Ticker", "Symbol"],
        "col_nom_candidates": ["Company", "Name"],
        "suffixe_yf": ".PA",
    },
    "DAX": {
        "symbole_yf": "^GDAXI",
        "wiki_url": "https://en.wikipedia.org/wiki/DAX",
        "col_ticker_candidates": ["Ticker", "Symbol"],
        "col_nom_candidates": ["Company", "Name"],
        "suffixe_yf": ".DE",
    },
}


@st.cache_data(ttl=86400)
def get_index_constituents(nom_indice):
    """
    Récupère la liste (ticker_yf, nom) des composants d'un indice via Wikipedia.
    Mis en cache 24h car la composition change rarement.
    NB : si la structure d'une page Wikipedia change, ajustez wiki_url /
    col_ticker_candidates / col_nom_candidates pour l'indice concerné.
    """
    cfg = INDICES_PRINCIPAUX[nom_indice]
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        resp = requests.get(cfg["wiki_url"], headers=headers, timeout=10)
        resp.raise_for_status()
        tables = pd.read_html(io.StringIO(resp.text))
    except Exception as e:
        st.error(f"Impossible de récupérer la composition de {nom_indice} : {e}")
        return []

    for table in tables:
        cols = [str(c).strip() for c in table.columns]
        col_ticker = next((c for c in cfg["col_ticker_candidates"] if c in cols), None)
        if not col_ticker:
            continue
        col_nom = next((c for c in cfg["col_nom_candidates"] if c in cols), None)

        tickers_bruts = table[col_ticker].astype(str).str.strip().tolist()
        noms = table[col_nom].astype(str).str.strip().tolist() if col_nom else tickers_bruts

        suffixe = cfg["suffixe_yf"]
        resultat = []
        for tk, nom in zip(tickers_bruts, noms):
            tk_propre = tk
            if suffixe:
                # Indices non-US : Wikipedia liste déjà le ticker Yahoo complet avec son
                # suffixe de place boursière (ex: "AC.PA", "SAP.DE", ou même "MT.AS" pour
                # ArcelorMittal coté à Amsterdam bien que membre du CAC 40) — on ne touche
                # pas aux tickers qui ont déjà un suffixe, on n'ajoute celui de l'indice
                # que si le ticker brut n'en a vraiment aucun.
                if "." not in tk_propre:
                    tk_propre = f"{tk_propre}{suffixe}"
            else:
                # Indices US : Yahoo utilise un tiret pour les classes d'actions
                # (ex: BRK.B -> BRK-B)
                tk_propre = tk_propre.replace(".", "-")
            resultat.append((tk_propre, nom))
        return resultat

    st.warning(f"Structure de page inattendue pour {nom_indice} — composants introuvables.")
    return []


@st.cache_data(ttl=300)
def get_index_market_data(tickers_noms, max_workers=8, _nonce=0):
    """
    Prix, variation du jour, variation YTD, volume et montant échangé.
    Récupération par titre (via yf.Ticker.history), en parallèle mais avec une
    concurrence limitée : Yahoo Finance rate-limite agressivement les rafales de
    requêtes simultanées, ce qui peut faire échouer TOUS les titres d'un coup
    même si chacun fonctionne bien pris isolément. Chaque titre est retenté une
    fois en cas d'échec transitoire.
    Le paramètre _nonce ne sert qu'à invalider le cache Streamlit sur demande
    (bouton "Réessayer").
    """
    noms_map = dict(tickers_noms)
    tickers = [t for t, n in tickers_noms]

    def _one(tk):
        derniere_erreur = "historique insuffisant"
        for tentative in range(2):
            try:
                hist = yf.Ticker(tk).history(period="ytd", interval="1d", auto_adjust=False)
                hist = hist.dropna(subset=["Close"])
                if len(hist) < 2:
                    time.sleep(0.3 * (tentative + 1))
                    continue
                prix             = float(hist["Close"].iloc[-1])
                prix_veille      = float(hist["Close"].iloc[-2])
                prix_debut_annee = float(hist["Close"].iloc[0])
                var_jour = (prix - prix_veille) / prix_veille * 100
                var_ytd  = (prix - prix_debut_annee) / prix_debut_annee * 100
                volume   = float(hist["Volume"].iloc[-1]) if pd.notna(hist["Volume"].iloc[-1]) else 0.0
                vol_serie = hist["Volume"].dropna()
                volume_moyen = float(vol_serie.mean()) if not vol_serie.empty else 0.0
                montant = volume * prix
                montant_moyen = float((hist["Close"] * hist["Volume"]).dropna().mean()) if not vol_serie.empty else 0.0
                return {
                    "Ticker": tk,
                    "Nom": noms_map.get(tk, tk),
                    "Prix": prix,
                    "VarJourNum": var_jour,
                    "VarYTDNum": var_ytd,
                    "Volume": volume,
                    "VolumeMoyen": volume_moyen,
                    "Montant": montant,
                    "MontantMoyen": montant_moyen,
                }, None
            except Exception as e:
                derniere_erreur = str(e)
                time.sleep(0.5 * (tentative + 1))
        return None, f"{tk} : {derniere_erreur}"

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        resultats = list(executor.map(_one, tickers))

    lignes  = [r for r, err in resultats if r is not None]
    erreurs = [err for r, err in resultats if err is not None]
    return pd.DataFrame(lignes), erreurs[:5]


@st.cache_data(ttl=3600)
def get_index_fondamentaux(tickers, max_workers=20):
    """Capitalisation, PER, PB — un appel .info par titre (plus lent, mis en cache 1h)."""

    def _one(tk):
        try:
            info = yf.Ticker(tk).info
            return {
                "Ticker": tk,
                "MarketCap": info.get("marketCap"),
                "PE": info.get("trailingPE"),
                "PB": info.get("priceToBook"),
            }
        except Exception:
            return {"Ticker": tk, "MarketCap": None, "PE": None, "PB": None}

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        resultats = list(executor.map(_one, tickers))
    return pd.DataFrame(resultats)


def style_heatmap_indice(df):
    """Coloration verte/rouge des colonnes de variation, cohérente avec style_df (Chg 1J...)."""
    styles = pd.DataFrame('', index=df.index, columns=df.columns)
    for col in ['Var Jour %', 'Var YTD %', 'Contrib. Jour %', 'Contrib. YTD %']:
        if col in df.columns:
            mask_plus  = df[col].astype(str).str.contains(r'\+')
            mask_moins = df[col].astype(str).str.contains('-')
            styles.loc[mask_plus,  col] += 'background-color: #d4edda; color: #155724; font-weight: bold;'
            styles.loc[mask_moins, col] += 'background-color: #f8d7da; color: #dc3545; font-weight: bold;'
    return styles


def afficher_graphique_indice(symbole_yf, nom_indice):
    """Graphique intraday de l'indice (prix + volume), avec en-tête façon 'ticker de marché'."""
    try:
        idx  = yf.Ticker(symbole_yf)
        hist = idx.history(period="1d", interval="1m")
        if hist.empty:
            hist = idx.history(period="5d", interval="15m")
        if hist.empty:
            st.info("Données intraday indisponibles pour le moment.")
            return

        try:
            prev_close = idx.fast_info.get("previous_close")
        except Exception:
            prev_close = None
        if not prev_close:
            prev_close = float(hist["Close"].iloc[0])

        dernier_prix = float(hist["Close"].iloc[-1])
        variation    = dernier_prix - prev_close
        var_pct      = (variation / prev_close) * 100
        couleur      = "#28a745" if var_pct >= 0 else "#dc3545"
        couleur_fill = "rgba(40,167,69,0.15)" if var_pct >= 0 else "rgba(220,53,69,0.15)"

        st.markdown(
            f"<div style='display:flex; align-items:baseline; gap:14px; flex-wrap:wrap;'>"
            f"<span style='font-size:1.4em; font-weight:bold;'>{nom_indice}</span>"
            f"<span style='font-size:1.8em; font-weight:bold;'>{dernier_prix:,.2f}</span>"
            f"<span style='font-size:1.3em; font-weight:bold; color:{couleur};'>{variation:+.2f}</span>"
            f"<span style='font-size:1.3em; font-weight:bold; color:{couleur};'>{var_pct:+.2f}%</span>"
            f"</div>", unsafe_allow_html=True
        )

        volume_dispo = "Volume" in hist.columns and hist["Volume"].fillna(0).sum() > 0

        if volume_dispo:
            fig = make_subplots(
                rows=2, cols=1, shared_xaxes=True,
                row_heights=[0.75, 0.25], vertical_spacing=0.03
            )
        else:
            fig = make_subplots(rows=1, cols=1)

        # Ligne de base à la clôture de la veille (trace invisible) + remplissage
        # relatif ('tonexty') entre cette ligne et le prix, au lieu d'un remplissage
        # vers zéro qui rend le graphique illisible sur des indices à forte valeur.
        fig.add_trace(go.Scatter(
            x=hist.index, y=[prev_close] * len(hist),
            mode="lines", line=dict(width=0), showlegend=False, hoverinfo="skip"
        ), row=1, col=1)

        fig.add_trace(go.Scatter(
            x=hist.index, y=hist["Close"], name="Prix",
            mode="lines", line=dict(color=couleur, width=1.5),
            fill="tonexty", fillcolor=couleur_fill
        ), row=1, col=1)

        fig.add_hline(
            y=prev_close, line_dash="dot", line_color="gray",
            annotation_text=f"Clôture veille {prev_close:,.2f}", row=1, col=1
        )

        if volume_dispo:
            fig.add_trace(go.Bar(
                x=hist.index, y=hist["Volume"], name="Volume",
                marker_color=couleur, opacity=0.3
            ), row=2, col=1)
            fig.update_yaxes(title_text="Volume", row=2, col=1)
        else:
            st.caption("ℹ️ Volume non communiqué par Yahoo Finance pour cet indice.")

        fig.update_layout(
            height=420, template="plotly_white", showlegend=False,
            margin=dict(l=10, r=10, t=20, b=10),
        )
        fig.update_yaxes(title_text="Prix", row=1, col=1)
        st.plotly_chart(
            fig,
            use_container_width=True,
            config={'scrollZoom': True, 'displaylogo': False}
        )

    except Exception as e:
        st.error(f"Erreur graphique indice : {e}")


def afficher_menu_indices():
    """Bloc sidebar : sélection d'un indice principal."""
    st.sidebar.header("📊 Indices Principaux")
    cols = st.sidebar.columns(2)
    noms = list(INDICES_PRINCIPAUX.keys())
    for i, nom_idx in enumerate(noms):
        with cols[i % 2]:
            if st.button(nom_idx, key=f"idx_btn_{nom_idx}", width="stretch"):
                st.session_state["vue_indice"] = nom_idx
                st.rerun()
    if st.session_state.get("vue_indice"):
        if st.sidebar.button("↩️ Retour au portefeuille", width="stretch"):
            st.session_state["vue_indice"] = None
            st.rerun()


def afficher_dashboard_indice(nom_indice):
    """Vue principale quand un indice est sélectionné : graphique + tableau des composants."""
    cfg = INDICES_PRINCIPAUX[nom_indice]

    afficher_graphique_indice(cfg["symbole_yf"], nom_indice)
    st.divider()

    with st.spinner(f"Récupération des composants de {nom_indice}..."):
        composants = get_index_constituents(nom_indice)
    if not composants:
        st.warning("Aucun composant récupéré.")
        return

    nb_composants = len(composants)
    st.caption(f"{nb_composants} valeurs")

    retry_key = f"retry_nonce_{nom_indice}"
    st.session_state.setdefault(retry_key, 0)

    with st.spinner("Récupération des cours..."):
        df, erreurs_marche = get_index_market_data(tuple(composants), _nonce=st.session_state[retry_key])
    if df.empty:
        st.warning("Données de marché indisponibles pour le moment.")
        if erreurs_marche:
            with st.expander("Détails techniques (aide au diagnostic)"):
                for e in erreurs_marche:
                    st.caption(e)
        if st.button("🔄 Réessayer", key=f"retry_{nom_indice}"):
            st.session_state[retry_key] += 1
            st.rerun()
        return

    charger_fondamentaux = st.checkbox(
        "Charger Poids / Capitalisation / PER / PB (plus lent sur les gros indices)",
        value=(nb_composants <= 50),
        key=f"fond_{nom_indice}"
    )

    if charger_fondamentaux:
        with st.spinner("Récupération des fondamentaux..."):
            df_fond = get_index_fondamentaux(df["Ticker"].tolist())
        df = df.merge(df_fond, on="Ticker", how="left")
        total_cap = df["MarketCap"].dropna().sum()
        df["PoidsNum"] = (df["MarketCap"] / total_cap * 100) if total_cap else None
        df["ContribJourNum"] = df["PoidsNum"] / 100 * df["VarJourNum"]
        df["ContribYTDNum"]  = df["PoidsNum"] / 100 * df["VarYTDNum"]

    # Tri par variation du jour, comme sur l'image de référence
    df = df.sort_values("VarJourNum", ascending=False).reset_index(drop=True)

    recherche = st.text_input("🔍 Filtrer par ticker ou nom", key=f"filtre_{nom_indice}")
    if recherche:
        q = recherche.lower()
        df = df[df["Ticker"].str.lower().str.contains(q) | df["Nom"].str.lower().str.contains(q)].reset_index(drop=True)

    # --- Colonnes d'affichage (formatage texte, cohérent avec le reste de l'appli) ---
    df_affiche = pd.DataFrame({
        "Ticker": df["Ticker"],
        "Nom": df["Nom"],
        "Prix": df["Prix"],
        "Var Jour %": df["VarJourNum"].apply(lambda v: f"{v:+.2f}%"),
        "Volume": df["Volume"],
        "Montant": df["Montant"],
        "Var YTD %": df["VarYTDNum"].apply(lambda v: f"{v:+.2f}%"),
    })

    if charger_fondamentaux:
        df_affiche.insert(2, "Poids %", df["PoidsNum"].apply(lambda v: f"{v:.2f}%" if pd.notna(v) else "N/A"))
        df_affiche["Contrib. Jour %"] = df["ContribJourNum"].apply(lambda v: f"{v:+.3f}%" if pd.notna(v) else "N/A")
        df_affiche["MarketCap"] = df["MarketCap"]
        df_affiche["Contrib. YTD %"] = df["ContribYTDNum"].apply(lambda v: f"{v:+.3f}%" if pd.notna(v) else "N/A")
        df_affiche["PE"] = df["PE"]
        df_affiche["PB"] = df["PB"]

    hauteur = min((len(df_affiche) * 35) + 38, 850)

    def style_volume_montant_indice(df_style):
        styles = pd.DataFrame('', index=df_style.index, columns=df_style.columns)
        for idx in df_style.index:
            try:
                if df.loc[idx, 'Volume'] > df.loc[idx, 'VolumeMoyen'] * 1.5:
                    styles.loc[idx, 'Volume'] = 'color: #cc0000;'
            except Exception:
                pass
            try:
                if df.loc[idx, 'Montant'] > df.loc[idx, 'MontantMoyen'] * 1.5:
                    styles.loc[idx, 'Montant'] = 'color: #cc0000;'
            except Exception:
                pass
        return styles

    sel = st.dataframe(
        df_affiche.style.apply(style_heatmap_indice, axis=None)
                         .apply(style_volume_montant_indice, axis=None).format(
            formatter=lambda x: clean_num(x) if isinstance(x, (int, float)) else x
        ),
        on_select="rerun",
        selection_mode="single-row",
        width="stretch",
        hide_index=True,
        height=hauteur,
    )

    # --- Détail fondamental au clic (réutilise fetch_stock_data + la fiche complète) ---
    if sel.selection and sel.selection.rows:
        ticker_choisi = df.iloc[sel.selection.rows[0]]["Ticker"]
        st.divider()
        with st.spinner(f"Analyse détaillée de {ticker_choisi}..."):
            detail = fetch_stock_data(ticker_choisi)
        if detail:
            afficher_detail_action(detail)
        else:
            st.info("Données fondamentales indisponibles pour cette valeur.")


# =======================================================================
# TABLEAU DE BORD MARCHÉ (INDICES GLOBAUX) — repris de indice.py
# =======================================================================

def _tdm_format_billions(val):
    if pd.isna(val) or not isinstance(val, (int, float, np.number)): return "N/A"
    return f"{val / 1e9:.2f}B"

def _tdm_format_smart_large_numbers(val):
    if pd.isna(val) or not isinstance(val, (int, float, np.number)): return "N/A"
    if val >= 1e9:
        return f"{val / 1e9:.2f}B"
    elif val >= 1e6:
        return f"{val / 1e6:.2f}M"
    return f"{val:.2f}"

def _tdm_format_thousands_int(val):
    if pd.isna(val) or not isinstance(val, (int, float, np.number)): return "N/A"
    return f"{int(round(val / 1e3))}K"

def _tdm_format_millions_int(val):
    if pd.isna(val) or not isinstance(val, (int, float, np.number)): return "N/A"
    return f"{int(round(val / 1e6))}M"


TDM_TICKERS_NASDAQ = [
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "GOOG", "TSLA", "AVGO", "COST",
    "PEP", "NFLX", "AMD", "QCOM", "ADBE", "TMUS", "CSCO", "INTU", "AMAT", "CMCSA",
    "TXN", "AMGN", "ISRG", "HON", "MU", "LRCX", "BKNG", "VRTX", "ADP", "MDLZ",
    "REGN", "INTC", "ADI", "PANW", "SNPS", "KLAC", "CDNS", "MAR", "CTAS", "ORLY"
]

TDM_TICKERS_CAC40 = [
    "MC.PA", "OR.PA", "RMS.PA", "TTE.PA", "SAN.PA", "AIR.PA", "SU.PA", "AI.PA",
    "BNP.PA", "DG.PA", "EL.PA", "SAF.PA", "SGO.PA", "STLA.PA", "DSY.PA", "CAP.PA",
    "ML.PA", "VIE.PA", "ORA.PA", "HO.PA", "ACA.PA", "GLE.PA", "KER.PA", "ENGI.PA",
    "PUB.PA", "VIV.PA", "RNO.PA", "EDEN.PA", "URW.PA", "EN.PA", "LR.PA", "WLN.PA"
]

# NOTE : compositions indicatives (hors S&P 500 complet à 500 lignes, non praticable
# en appels yfinance individuels). À ajuster si besoin.
TDM_TICKERS_DAX = [
    "ADS.DE", "ALV.DE", "BAS.DE", "BAYN.DE", "BEI.DE", "BMW.DE", "BNR.DE", "CON.DE",
    "1COV.DE", "DBK.DE", "DB1.DE", "DHL.DE", "DTE.DE", "EOAN.DE", "FRE.DE", "HNR1.DE",
    "HEI.DE", "HEN3.DE", "IFX.DE", "MBG.DE", "MRK.DE", "MTX.DE", "MUV2.DE", "PAH3.DE",
    "P911.DE", "QIA.DE", "RHM.DE", "RWE.DE", "SAP.DE", "SRT3.DE", "SIE.DE", "ENR.DE",
    "SHL.DE", "SY1.DE", "VOW3.DE", "VNA.DE", "ZAL.DE"
]

TDM_TICKERS_SP500 = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "GOOG", "META", "BRK-B", "LLY", "AVGO",
    "JPM", "TSLA", "UNH", "V", "XOM", "MA", "JNJ", "PG", "HD", "MRK",
    "COST", "ABBV", "CVX", "PEP", "KO", "WMT", "BAC", "CRM", "ADBE", "MCD",
    "TMO", "CSCO", "ABT", "NFLX", "ACN", "LIN", "DHR", "PFE", "DIS", "WFC"
]

TDM_TICKERS_NIKKEI = [
    "7203.T", "6758.T", "9984.T", "6861.T", "8306.T", "6098.T", "9432.T", "6902.T",
    "7267.T", "4063.T", "8035.T", "6501.T", "6367.T", "4519.T", "9433.T", "7974.T",
    "8058.T", "8001.T", "8316.T", "4568.T", "6273.T", "6954.T", "9983.T", "4543.T",
    "7741.T", "6503.T", "5108.T", "8411.T", "7011.T", "4661.T"
]

TDM_TICKERS_EUROSTOXX50 = [
    "ASML.AS", "SAP.DE", "MC.PA", "TTE.PA", "SIE.DE", "ALV.DE", "AIR.PA", "SAN.PA",
    "IBE.MC", "OR.PA", "BAYN.DE", "SU.PA", "BAS.DE", "DTE.DE", "MBG.DE", "INGA.AS",
    "MUV2.DE", "ABI.BR", "ADYEN.AS", "BNP.PA", "ENEL.MI", "ENI.MI", "ISP.MI",
    "PHIA.AS", "VOW3.DE", "DB1.DE", "KER.PA", "NOKIA.HE", "VNA.DE", "RMS.PA",
    "DG.PA", "EL.PA", "BBVA.MC", "SAF.PA", "DHL.DE"
]

# NOTE : compositions indicatives pour les indices suivants (non praticable de lister
# l'intégralité des composants — Russell 2000 = ~2000 valeurs, MSCI World/EM = ~1500-3000
# valeurs multi-pays — voir plus haut pour la même remarque sur le DAX / S&P 500).
TDM_TICKERS_FTSE100 = [
    "AZN.L", "SHEL.L", "HSBA.L", "ULVR.L", "BP.L", "GSK.L", "DGE.L", "RIO.L",
    "BATS.L", "REL.L", "LSEG.L", "NG.L", "GLEN.L", "AAL.L", "BA.L", "RR.L",
    "VOD.L", "LLOY.L", "BARC.L", "PRU.L", "CPG.L", "NWG.L", "SSE.L", "SGE.L",
    "IMB.L", "TSCO.L", "ABF.L", "CRH.L", "STAN.L", "III.L", "SMIN.L", "AV.L",
    "LGEN.L", "WTB.L"
]

TDM_TICKERS_RUSSELL2000 = [
    "SFM", "CVLT", "FN", "WING", "CROX", "LNTH", "BOOT", "CALM", "SPSC", "ESE",
    "ATRC", "HALO", "MMSI", "POWI", "ONTO", "EXLS", "PLXS", "KTB", "FIZZ", "CARG",
    "SAIA", "AXON", "CHRD", "CRVL", "IBP", "MEDP", "TMHC", "UFPT", "VC", "WERN"
]

TDM_TICKERS_MSCI_WORLD = [
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "AVGO", "TSLA", "JPM", "LLY",
    "ASML.AS", "NOVN.SW", "ROG.SW", "NESN.SW", "SAP.DE", "MC.PA", "TTE.PA", "SIE.DE",
    "AZN.L", "SHEL.L", "HSBA.L", "ULVR.L",
    "7203.T", "6758.T", "9984.T", "8306.T",
    "RY.TO", "SHOP.TO", "CNQ.TO",
    "BHP.AX", "CBA.AX", "CSL.AX"
]

TDM_TICKERS_MSCI_EM = [
    "TSM", "BABA", "PDD", "JD",
    "0700.HK", "9988.HK", "3690.HK", "1299.HK", "0939.HK", "2318.HK", "1810.HK", "1211.HK",
    "005930.KS", "000660.KS",
    "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "INFY.NS", "ICICIBANK.NS",
    "2330.TW", "2317.TW",
    "VALE", "PBR", "ITUB",
    "NPN.JO"
]

TDM_TICKERS_FTSEMIB = [
    "ENI.MI", "ISP.MI", "UCG.MI", "ENEL.MI", "RACE.MI", "STLAM.MI", "G.MI", "MB.MI",
    "BAMI.MI", "PRY.MI", "REC.MI", "SRG.MI", "TRN.MI", "MONC.MI", "DIA.MI", "AMP.MI",
    "BPE.MI", "IP.MI", "LDO.MI", "TEN.MI", "INW.MI", "HER.MI", "NEXI.MI", "FBK.MI",
    "BMED.MI", "IG.MI", "STMMI.MI", "PST.MI"
]

TDM_TICKERS_NIFTY50 = [
    "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "INFY.NS", "ICICIBANK.NS", "HINDUNILVR.NS",
    "ITC.NS", "SBIN.NS", "BHARTIARTL.NS", "KOTAKBANK.NS", "LT.NS", "BAJFINANCE.NS",
    "AXISBANK.NS", "ASIANPAINT.NS", "MARUTI.NS", "HCLTECH.NS", "SUNPHARMA.NS", "TITAN.NS",
    "ULTRACEMCO.NS", "WIPRO.NS", "NESTLEIND.NS", "ONGC.NS", "NTPC.NS", "POWERGRID.NS",
    "M&M.NS", "TATASTEEL.NS", "TATAMOTORS.NS", "ADANIENT.NS", "ADANIPORTS.NS", "JSWSTEEL.NS",
    "INDUSINDBK.NS", "BAJAJFINSV.NS", "HDFCLIFE.NS", "SBILIFE.NS", "GRASIM.NS", "CIPLA.NS",
    "DRREDDY.NS", "EICHERMOT.NS", "BPCL.NS", "COALINDIA.NS", "HEROMOTOCO.NS", "BRITANNIA.NS",
    "DIVISLAB.NS", "APOLLOHOSP.NS", "TECHM.NS", "UPL.NS", "BAJAJ-AUTO.NS", "HINDALCO.NS",
    "SHREECEM.NS", "LTIM.NS"
]

TDM_TICKERS_FTSECHINA50 = [
    "0700.HK", "9988.HK", "3690.HK", "0939.HK", "1398.HK", "3988.HK", "0388.HK",
    "2318.HK", "0941.HK", "1288.HK", "0857.HK", "0386.HK", "2628.HK", "1810.HK",
    "9618.HK", "9999.HK", "2020.HK", "1211.HK", "0175.HK", "2382.HK", "1093.HK",
    "0027.HK", "1109.HK", "0016.HK", "0011.HK"
]

TDM_INDEX_TICKER_MAP = {
    "NASDAQ 100 Tech Heavyweights": TDM_TICKERS_NASDAQ,
    "CAC 40 (France)": TDM_TICKERS_CAC40,
    "DAX (Allemagne)": TDM_TICKERS_DAX,
    "S&P 500 (USA)": TDM_TICKERS_SP500,
    "Nikkei 225 (Japon)": TDM_TICKERS_NIKKEI,
    "EURO STOXX 50": TDM_TICKERS_EUROSTOXX50,
    "FTSE 100 (Royaume-Uni)": TDM_TICKERS_FTSE100,
    "Russell 2000 (États-Unis)": TDM_TICKERS_RUSSELL2000,
    "MSCI World": TDM_TICKERS_MSCI_WORLD,
    "MSCI Emerging Markets": TDM_TICKERS_MSCI_EM,
    "FTSE MIB (Italie)": TDM_TICKERS_FTSEMIB,
    "NIFTY 50 (Inde)": TDM_TICKERS_NIFTY50,
    "FTSE China 50": TDM_TICKERS_FTSECHINA50,
}

# Symbole de l'indice lui-même (pour le graphique intraday agrégé)
# NOTE : pour MSCI World / MSCI Emerging Markets / FTSE China 50, Yahoo Finance ne propose
# pas de ticker d'indice directement exploitable en intraday : on utilise l'ETF de référence
# le plus liquide qui réplique l'indice (URTH, EEM, FXI) comme proxy du niveau/variation.
TDM_SYMBOL_MAP = {
    "NASDAQ 100 Tech": "^NDX",
    "CAC 40 (France)": "^FCHI",
    "DAX (Allemagne)": "^GDAXI",
    "S&P 500 (USA)": "^GSPC",
    "Nikkei 225 (Japon)": "^N225",
    "EURO STOXX 50": "^STOXX50E",
    "FTSE 100 (Royaume-Uni)": "^FTSE",
    "Russell 2000 (États-Unis)": "^RUT",
    "MSCI World": "URTH",
    "MSCI Emerging Markets": "EEM",
    "FTSE MIB (Italie)": "^FTSEMIB",
    "NIFTY 50 (Inde)": "^NSEI",
    "FTSE China 50": "FXI",
}


@st.cache_data(ttl=60)  # Rafraîchissement toutes les minutes pour l'intraday
def tdm_get_intraday_data(ticker_symbol):
    ticker = yf.Ticker(ticker_symbol)
    hist = ticker.history(period="1d", interval="5m")
    return hist


@st.cache_data(ttl=1800)
def tdm_get_ytd_data(ticker_symbol):
    ticker = yf.Ticker(ticker_symbol)
    hist = ticker.history(period="ytd", interval="1d")
    return hist


def _tdm_build_combined_chart(df, reference_price, chart_title, x_title):
    """Construit un graphique unique combinant le prix (ligne, axe gauche)
    et le volume (barres, axe droit) sur le même panneau."""
    df = df.copy()
    window = max(3, len(df) // 15)
    df['MA'] = df['Close'].rolling(window=window, min_periods=1).mean()
    df['Pct'] = ((df['Close'] - reference_price) / reference_price) * 100 if reference_price else 0
    vol_colors = np.where(df['Close'] >= df['Open'], '#90b8e8', '#e89a9a')
    vol_max = float(df['Volume'].max()) if not df['Volume'].dropna().empty else 1.0

    fig = go.Figure()

    fig.add_trace(go.Bar(
        x=df.index, y=df['Volume'], name='Volume',
        marker_color=vol_colors, opacity=0.45, yaxis='y2'
    ))

    fig.add_trace(go.Scatter(
        x=df.index, y=[reference_price] * len(df), mode='lines',
        line=dict(width=0), showlegend=False, hoverinfo='skip'
    ))

    fig.add_trace(go.Scatter(
        x=df.index,
        y=df['Close'],
        mode='lines',
        name='Prix',
        line=dict(color='#2b6cb0', width=1.8),
        fill='tonexty',
        fillcolor='rgba(144,184,232,0.35)',
        customdata=df['Pct'],
        hovertemplate="Prix: %{y:,.2f}<br>Variation: %{customdata:.2f}%<extra></extra>"
    ))

    fig.add_trace(go.Scatter(
        x=df.index, y=df['MA'],
        mode='lines', name='Tendance (moy. mobile)',
        line=dict(color='#e07b39', width=1.6)
    ))

    fig.update_layout(
        title=chart_title,
        height=600,
        hovermode='x unified',
        legend=dict(orientation='h', yanchor='bottom', y=1.02, xanchor='right', x=1),
        margin=dict(t=60, b=40),
        xaxis=dict(title=x_title),
        yaxis=dict(title="Prix"),
        yaxis2=dict(
            title="Volume", overlaying='y', side='right',
            showgrid=False, range=[0, vol_max * 4]
        )
    )
    return fig


@st.cache_data(ttl=60)
def tdm_get_index_daily_changes():
    """Variation du jour (dernier cours vs clôture veille) pour chaque indice,
    utilisée pour l'affichage rapide dans le menu latéral."""
    changes = {}
    symbols = list(dict.fromkeys(TDM_SYMBOL_MAP.values()))
    try:
        data = yf.download(symbols, period="5d", progress=False)['Close']
        if isinstance(data, pd.Series):
            data = data.to_frame(name=symbols[0])
        for name, sym in TDM_SYMBOL_MAP.items():
            try:
                s = data[sym].dropna()
                if len(s) >= 2:
                    changes[name] = ((s.iloc[-1] - s.iloc[-2]) / s.iloc[-2]) * 100
            except Exception:
                continue
    except Exception:
        pass
    return changes


@st.cache_data(ttl=300)
def tdm_get_previous_close(ticker_symbol, fallback):
    try:
        info = yf.Ticker(ticker_symbol).info
        pc = info.get("previousClose")
        if pc and isinstance(pc, (int, float)):
            return float(pc)
    except Exception:
        pass
    return float(fallback)


@st.cache_data(ttl=300)
def tdm_get_advanced_market_data(tickers):
    df_prices = yf.download(tickers, start="2026-01-01", progress=False)
    if isinstance(df_prices.columns, pd.MultiIndex):
        df_prices.columns = df_prices.columns.remove_unused_levels()

    rows = []
    for t in tickers:
        try:
            ticker_obj = yf.Ticker(t)
            info = ticker_obj.info

            if isinstance(df_prices.columns, pd.MultiIndex):
                close_series = df_prices['Close', t].dropna() if ('Close', t) in df_prices.columns else pd.Series()
                open_series = df_prices['Open', t].dropna() if ('Open', t) in df_prices.columns else pd.Series()
                vol_series = df_prices['Volume', t].dropna() if ('Volume', t) in df_prices.columns else pd.Series()
            else:
                close_series = df_prices['Close'][t].dropna() if t in df_prices['Close'].columns else pd.Series()
                open_series = df_prices['Open'][t].dropna() if t in df_prices['Open'].columns else pd.Series()
                vol_series = df_prices['Volume'][t].dropna() if t in df_prices['Volume'].columns else pd.Series()

            if close_series.empty: continue

            p_ytd_start = float(close_series.iloc[0])
            p_prev_close = float(close_series.iloc[-2]) if len(close_series) > 1 else float(close_series.iloc[-1])
            p_latest = float(close_series.iloc[-1])
            p_open_today = float(open_series.iloc[-1]) if not open_series.empty else p_latest

            volume_actuel = int(vol_series.iloc[-1]) if not vol_series.empty else 0
            volume_moyen = vol_series.mean() if not vol_series.empty else 0

            montant_actuel = p_latest * volume_actuel
            montant_moyen = (close_series * vol_series).mean() if not vol_series.empty else 0

            reg_time = info.get('regularMarketTime')
            if reg_time and isinstance(reg_time, (int, float)):
                timestamp_latest = datetime.fromtimestamp(reg_time).strftime('%Y-%m-%d %H:%M:%S')
            else:
                timestamp_latest = close_series.index[-1].strftime('%Y-%m-%d 16:00:00')

            div_rate = info.get('dividendRate')
            if div_rate and isinstance(div_rate, (int, float)) and p_latest > 0:
                dividend_yield = (div_rate / p_latest) * 100
            else:
                div_yield_raw = info.get('dividendYield', np.nan)
                if not pd.isna(div_yield_raw):
                    dividend_yield = div_yield_raw if div_yield_raw > 1.0 else div_yield_raw * 100
                else:
                    dividend_yield = np.nan

            rows.append({
                "Ticker": t,
                "Name": info.get('longName', t),
                "Price": p_prev_close,
                "IntradayReturn": ((p_latest - p_prev_close) / p_prev_close) * 100,
                "Price_Latest": p_latest,
                "Return_Latest": ((p_latest - p_open_today) / p_open_today) * 100 if p_open_today != 0 else 0,

                "Volume_Raw": volume_actuel,
                "Volume_Moyen_Raw": volume_moyen,
                "Amount_Raw": montant_actuel,
                "Amount_Moyen_Raw": montant_moyen,

                "MarketCap_Raw": info.get('marketCap', 0),
                "YTDReturn": ((p_latest - p_ytd_start) / p_ytd_start) * 100,
                "PE": info.get('trailingPE', np.nan),
                "PB": info.get('priceToBook', np.nan),
                "Profit_TTM": _tdm_format_billions(info.get('netIncomeToCommon', np.nan)),
                "DividendYield": dividend_yield,
                "Dividend": div_rate if div_rate else np.nan,
                "SharesOutstanding_Raw": info.get('sharesOutstanding', np.nan),
                "Exchange": info.get('exchange', 'N/A'),
                "Timestamp_Latest": timestamp_latest
            })
        except:
            continue

    df = pd.DataFrame(rows)
    if df.empty: return df

    total_mcap = df['MarketCap_Raw'].sum()
    df['Weight'] = (df['MarketCap_Raw'] / total_mcap) * 100 if total_mcap > 0 else 0
    df['IntradayContribution'] = (df['Weight'] * df['IntradayReturn']) / 100
    df['YTDContribution'] = (df['Weight'] * df['YTDReturn']) / 100

    df['Volume'] = df['Volume_Raw'].apply(_tdm_format_thousands_int)
    df['VolumeMoyen'] = df['Volume_Moyen_Raw'].apply(_tdm_format_thousands_int)
    df['Amount'] = df['Amount_Raw'].apply(_tdm_format_millions_int)
    df['AmountMoyen'] = df['Amount_Moyen_Raw'].apply(_tdm_format_millions_int)

    df['MarketCap'] = df['MarketCap_Raw'].apply(_tdm_format_billions)
    df['SharesOutstanding'] = df['SharesOutstanding_Raw'].apply(_tdm_format_smart_large_numbers)

    ordered_cols = [
        "Ticker", "Name", "Weight", "Price", "IntradayReturn", "Price_Latest",
        "Return_Latest", "Volume", "VolumeMoyen", "Amount", "AmountMoyen", "IntradayContribution",
        "MarketCap", "YTDReturn", "YTDContribution", "PE", "PB", "Profit_TTM",
        "DividendYield", "Dividend", "SharesOutstanding", "Exchange", "Timestamp_Latest",
        "Volume_Raw", "Volume_Moyen_Raw", "Amount_Raw", "Amount_Moyen_Raw"
    ]
    df = df[ordered_cols]

    fr_cols = {
        "Ticker": "Ticker", "Name": "Nom", "Weight": "Poids", "Price": "Prix Veille",
        "IntradayReturn": "Var. Intraday (%)", "Price_Latest": "Dernier Prix",
        "Return_Latest": "Var. Session (%)",
        "Volume": "Volume", "VolumeMoyen": "Volume Moyen",
        "Amount": "Montant", "AmountMoyen": "Montant Moyen",
        "IntradayContribution": "Influence Intraday",
        "MarketCap": "Capitalisation",
        "YTDReturn": "Var. YTD (%)", "YTDContribution": "Contrib. YTD", "PE": "PER",
        "PB": "P/B", "Profit_TTM": "Bénéfice TTM", "DividendYield": "Rend. Dividende",
        "Dividend": "Dividende", "SharesOutstanding": "Actions en Circ.",
        "Exchange": "Bourse", "Timestamp_Latest": "Dernier Horodatage"
    }
    return df.rename(columns=fr_cols)


def afficher_tableau_de_bord_marche():
    """Page complète 'Tableau de Bord Marché' — reprend le contenu de indice.py."""

    st.sidebar.header("📌 Configuration Marché")
    show_charts = st.sidebar.checkbox("Afficher les graphiques", value=True, key="tdm_show_charts")
    show_ytd = st.sidebar.checkbox(
        "Évolution sur l'année en cours (YTD)",
        value=False,
        key="tdm_show_ytd",
        help="Basculer le graphique en données journalières depuis le 1er janvier, au lieu de l'intraday"
    )

    index_daily_changes = tdm_get_index_daily_changes()

    def _label_avec_variation(nom):
        pct = index_daily_changes.get(nom)
        if pct is None:
            return nom
        emoji = "🟢" if pct >= 0 else "🔴"
        return f"{nom}  {emoji} {pct:+.2f}%"

    selected_index = st.sidebar.radio(
        "Indices disponibles :",
        list(TDM_INDEX_TICKER_MAP.keys()),
        key="tdm_selected_index",
        format_func=_label_avec_variation
    )
    current_tickers = TDM_INDEX_TICKER_MAP[selected_index]

    st.title("📈 Tableau de Bord d'Indicateurs Financiers")

    with st.spinner("Chargement des données..."):
        df_data = tdm_get_advanced_market_data(current_tickers)

    if show_charts:
        target_symbol = TDM_SYMBOL_MAP.get(selected_index)

        if show_ytd:
            df_chart = tdm_get_ytd_data(target_symbol)
            x_title = "Date"
            titre_periode = "Évolution du prix et du volume — Année en cours (YTD)"
        else:
            df_chart = tdm_get_intraday_data(target_symbol)
            x_title = "Heure"
            titre_periode = "Prix et Volume Intraday"

        if not df_chart.empty:
            if show_ytd:
                reference_price = float(df_chart['Close'].iloc[0])
            else:
                reference_price = tdm_get_previous_close(target_symbol, df_chart['Close'].iloc[0])

            last_price = float(df_chart['Close'].iloc[-1])
            change_abs = last_price - reference_price
            change_pct = (change_abs / reference_price) * 100 if reference_price else 0
            color_header = "#006622" if change_abs >= 0 else "#cc0000"

            st.markdown(
                f"""
                <div style="display:flex; align-items:baseline; gap:14px; margin-bottom:2px;">
                    <span style="font-size:20px; font-weight:700;">{selected_index}</span>
                    <span style="font-size:26px; font-weight:800; color:{color_header};">{last_price:,.2f}</span>
                    <span style="font-size:16px; font-weight:600; color:{color_header};">{change_abs:+.2f}</span>
                </div>
                <div style="font-size:18px; font-weight:700; color:{color_header}; margin-bottom:6px;">
                    {change_pct:+.2f}%
                </div>
                """,
                unsafe_allow_html=True
            )

            fig = _tdm_build_combined_chart(
                df_chart,
                reference_price,
                f"{selected_index} — {titre_periode}",
                x_title
            )

            st.plotly_chart(
                fig,
                use_container_width=True,
                config={
                    'scrollZoom': True,
                    'displayModeBar': True,
                    'modeBarButtonsToAdd': ['drawline', 'drawrect', 'eraseshape'],
                    'displaylogo': False
                }
            )
        else:
            st.warning("Données indisponibles pour cet indice.")

    if not df_data.empty:

        def style_market_colors(val):
            try:
                v = float(val)
                if v > 2.0: return 'background-color: #e6ffec; color: #006622; font-weight: bold;'
                elif v > 0: return 'background-color: #e6ffec; color: #006622;'
                elif v < -2.0: return 'background-color: #ffe6e6; color: #cc0000; font-weight: bold;'
                elif v < 0: return 'background-color: #ffe6e6; color: #cc0000;'
                return ''
            except:
                return ''

        def style_volume_amount_alerts(df):
            style_df = pd.DataFrame('', index=df.index, columns=df.columns)
            for idx in df.index:
                if df.loc[idx, 'Volume_Raw'] > df.loc[idx, 'Volume_Moyen_Raw'] * 1.5:
                    style_df.loc[idx, 'Volume'] = 'color: #cc0000;'
                if df.loc[idx, 'Amount_Raw'] > df.loc[idx, 'Amount_Moyen_Raw'] * 1.5:
                    style_df.loc[idx, 'Montant'] = 'color: #cc0000;'
            return style_df

        df_styled = df_data.style\
            .map(style_market_colors, subset=['Var. Intraday (%)', 'Var. Session (%)', 'Var. YTD (%)'])\
            .apply(style_volume_amount_alerts, axis=None)\
            .bar(subset=['Poids'], color='#4a90e2', vmin=0, vmax=float(df_data['Poids'].max()))\
            .format({
                'Prix Veille': '{:.2f}',
                'Dernier Prix': '{:.2f}',
                'Var. Intraday (%)': '{:+.2f}%',
                'Var. Session (%)': '{:+.2f}%',
                'Poids': '{:.2f}%',
                'Influ. Intraday': '{:+.2f}%',
                'Var. YTD (%)': '{:+.2f}%',
                'Influ. YTD': '{:+.2f}%',
                'PER': '{:.2f}',
                'P/B': '{:.2f}',
                'Rend. Dividende': '{:.2f}%',
                'Dividende': '{:.2f}'
            }, na_rep="N/A")

        cols_to_display = [
            "Ticker", "Nom", "Poids", "Prix Veille", "Var. Intraday (%)", "Dernier Prix",
            "Var. Session (%)", "Volume", "Volume Moyen", "Montant", "Montant Moyen",
            "Influence Intraday", "Capitalisation", "Var. YTD (%)", "Contrib. YTD",
            "PER", "P/B", "Bénéfice TTM", "Rend. Dividende", "Dividende", "Actions en Circ.",
            "Bourse", "Dernier Horodatage"
        ]

        st.caption("💡 Cliquez sur une ligne pour afficher la fiche détaillée de l'action.")

        sel_marche = st.dataframe(
            df_styled,
            use_container_width=True,
            hide_index=True,
            height=600,
            column_order=cols_to_display,
            column_config={
                "Nom": st.column_config.Column(pinned=True)
            },
            on_select="rerun",
            selection_mode="single-row",
        )

        if sel_marche.selection and sel_marche.selection.rows:
            ticker_choisi = df_data.iloc[sel_marche.selection.rows[0]]["Ticker"]
            st.divider()
            with st.spinner(f"Chargement de la fiche détaillée de {ticker_choisi}..."):
                d_detail = fetch_stock_data(ticker_choisi)
            if d_detail:
                afficher_detail_action(d_detail)
            else:
                st.warning("Données détaillées indisponibles pour cette valeur.")
    else:
        st.error("Aucune donnée disponible.")

# =======================================================================
# GESTION LISTES & COLONNES
# =======================================================================

@st.cache_data(ttl=3600)
def get_all_watchlists():
    try:
        conn = st.connection("gsheets", type=GSheetsConnection)
        df   = conn.read(worksheet="Watchlists")
        watchlists_dict = {}
        if not df.empty and 'list_name' in df.columns:
            col_ticker = 'tickers' if 'tickers' in df.columns else 'Ticker'
            for name in df['list_name'].dropna().unique():
                t_data      = df[df['list_name'] == name][col_ticker].iloc[0]
                ticker_list = [t.strip().upper() for t in str(t_data).split(',') if t.strip()]
                watchlists_dict[name] = ticker_list
            return watchlists_dict
        return {"Actions_EU": ["AAPL"]}
    except:
        return {"Actions_EU": ["AAPL"]}


def delete_watchlist_gsheets(watchlist_name):
    conn       = st.connection("gsheets", type=GSheetsConnection)
    df         = conn.read(worksheet="Watchlists")
    df_updated = df[df['Wallet_Name'] != watchlist_name]
    conn.update(worksheet="Watchlists", data=df_updated)
    st.cache_data.clear()


def load_watchlist_gsheets(list_name):
    try:
        conn = st.connection("gsheets", type=GSheetsConnection)
        df   = conn.read(worksheet="Watchlists")
        res  = df[df['list_name'] == list_name]
        if not res.empty:
            return res.iloc[0]['tickers']
        return ""
    except Exception:
        return ""


def save_watchlist_gsheets(list_name, tickers_text):
    try:
        conn = st.connection("gsheets", type=GSheetsConnection)
        df   = conn.read(worksheet="Watchlists")
        if list_name in df['list_name'].values:
            df.loc[df['list_name'] == list_name, 'tickers'] = tickers_text
        else:
            new_row = pd.DataFrame({'list_name': [list_name], 'tickers': [tickers_text]})
            df = pd.concat([df, new_row], ignore_index=True)
        conn.update(worksheet="Watchlists", data=df)
        st.success(f"✅ Liste '{list_name}' synchronisée !")
    except Exception as e:
        st.error(f"Erreur de sauvegarde : {e}")


def on_list_change():
    st.cache_data.clear()
    if "ticker_editor" in st.session_state:
        st.session_state.ticker_editor = load_watchlist_gsheets(st.session_state.sel_list)
    st.cache_data.clear()


# =======================================================================
# INTERFACE PRINCIPALE
# =======================================================================

st.set_page_config(page_title="Analyseur Pro+", layout="wide")
st.markdown("""
    <style>
    .block-container { padding-top: 3.5rem !important; }
    [data-testid="stSidebarNav"] {padding-top: 0rem;}
    [data-testid="stSidebarContent"] > div:first-child {padding-top: 1rem;}
    [data-testid='stTable'] {font-size: 13px;}
    .stVerticalBlock {gap: 0.5rem;}
    </style>
""", unsafe_allow_html=True)

# --- MENU DE NAVIGATION (SIDEBAR) ---
with st.sidebar:
    page_actuelle = st.radio(
        "🧭 Navigation",
        ["📁 Portefeuille", "🌍 Indices", "📰 Actualités"],
        index=2,
        key="page_actuelle",
    )
    st.divider()

show_news_portfolio = (page_actuelle == "📰 Actualités")

# =======================================================================
# PAGE : TABLEAU DE BORD MARCHÉ
# =======================================================================
if page_actuelle == "🌍 Indices":
    afficher_tableau_de_bord_marche()
    st.stop()

# =======================================================================
# PAGE : PORTEFEUILLE (comportement existant)
# =======================================================================
with st.sidebar:
    if st.button("🔄 Forcer l'actualisation", width="stretch"):
        st.cache_data.clear()
        st.rerun()

    st.divider()
    st.header("🔍 Recherche d'Action")
    sq = st.text_input("Nom de la société (ex: LVMH)")
    if sq:
        sug = search_ticker(sq)
        if sug:
            opt     = [x['label'] for x in sug]
            sel_opt = st.selectbox("Résultats :", opt)
            tk_add  = sug[opt.index(sel_opt)]['symbol']
            if st.button(f"➕ Ajouter {tk_add}"):
                cur_tk          = load_watchlist_gsheets(st.session_state.get('sel_list', 'Portefeuille Principal'))
                new_tickers_list = cur_tk + f", {tk_add}"
                save_watchlist_gsheets(st.session_state.get('sel_list', 'Portefeuille Principal'), new_tickers_list)
                st.session_state["ticker_editor"] = new_tickers_list
                st.cache_data.clear()
                st.rerun()

    st.divider()
    st.header("📂 Portefeuilles")
    lists    = get_all_watchlists()
    sel_list = st.selectbox("Liste active :", options=list(lists.keys()), key='sel_list', on_change=on_list_change)

    col1, col2 = st.columns(2)
    with col1:
        show_add = st.toggle("➕ Créer")
    with col2:
        show_del = st.toggle("🗑️", help="Supprimer un portefeuille")

    if show_add:
        st.info("Créer une nouvelle liste")
        new_name = st.text_input("Nom de la liste :", placeholder="Ex: Dividendes")
        if st.button("Confirmer Création", width="stretch"):
            if new_name:
                save_watchlist_gsheets(new_name, "AAPL")
                st.success(f"'{new_name}' Liste créée !")
                st.cache_data.clear()
                time.sleep(0.5)
                st.rerun()
            else:
                st.error("Nom vide !")

    if show_del:
        st.warning("⚠️ Action irréversible")
        list_to_del = st.selectbox("Choisir la liste à supprimer :", lists, key="del_select_box")
        if st.button(f"Confirmer la suppression de {list_to_del}", type="primary", key="btn_confirm_del"):
            if len(lists) > 1:
                try:
                    conn       = st.connection("gsheets", type=GSheetsConnection)
                    df_all     = conn.read(worksheet="Watchlists")
                    df_updated = df_all[df_all['list_name'] != list_to_del]
                    conn.update(worksheet="Watchlists", data=df_updated)
                    st.success(f"🔥 Liste '{list_to_del}' supprimée avec succès !")
                    st.cache_data.clear()
                    time.sleep(0.5)
                    st.rerun()
                except Exception as e:
                    st.error(f"Erreur lors de la suppression : {e}")
            else:
                st.error("🚫 Impossible de supprimer la dernière liste !")

    st.divider()
    afficher_menu_indices()

    st.divider()

    current_content = load_watchlist_gsheets(sel_list)
    if "ticker_editor" not in st.session_state:
        st.session_state["ticker_editor"] = current_content

    tickers_input = st.text_area(
        "Éditer les tickers :",
        value=current_content,
        height=100,
        key="ticker_editor",
        on_change=update_tickers_callback
    ).upper()
    st.divider()

    # Colonnes disponibles (sans les colonnes internes)
    cols_all = [
        "Nom", "Secteur", "Prix Actuel",
        "BNA Actuel", "PER Actuel", "PEG Actuel", "PEG Forward",
        "BNA Forward", "PER Forward",
        "ROA", "ROE", "Marge Nette", "Dette/Equity", "Beta",
        "Croissance EBITDA", "P/FCF", "P/FCF Moy 3a",   # NOUVEAU
        "CAGR 3 ans", "CAGR 5 ans",
        "Entrée BNA -15%", "Entrée FCF -15%", "Entrée Analystes -15%", "Entrée Synthèse (-15%)",
        "Santé (Piotroski)",
        "Chg 1J", "Chg 1M", "Chg YTD",
        "Nb Analystes", "Dividende (€/$)", "Rendement %", "Date Détachement",
        "Date Versement Dividende", "Prochains Résultats", "Avis Analystes"
    ]

# =======================================================================
# VUE INDICE (si un indice est sélectionné dans la sidebar)
# =======================================================================
if st.session_state.get("vue_indice"):
    afficher_dashboard_indice(st.session_state["vue_indice"])
    st.stop()

# --- TITRE ---
st.title(f"📈 {sel_list}")
t_list = [t.strip().upper() for t in tickers_input.replace('\r', '').replace('\n', ',').split(',') if t.strip()]


@st.cache_data(ttl=3600)
def get_column_config():
    return conn.read(worksheet="Choix_colonnes")


# =======================================================================
# CHARGEMENT DES DONNÉES
# =======================================================================

if t_list:
    status_container = st.empty()
    with status_container.container():
        with st.status(f"⏳ Analyse de {len(t_list)} actions en cours...", expanded=True) as status:
            st.write("Connexion aux serveurs financiers...")
            with ThreadPoolExecutor(max_workers=20) as executor:
                results = list(executor.map(fetch_stock_data, t_list))
            st.write("Finalisation des calculs...")
            status.update(label="✅ Données prêtes !", state="complete", expanded=False)
            time.sleep(0.5)

    status_container.empty()
    data_res = [r for r in results if r is not None]

    if data_res:
        df = pd.DataFrame(data_res)
        df['Date Détachement'] = pd.to_datetime(df['Date Détachement'], errors='coerce', dayfirst=True)
        if 'Date Versement Dividende' in df.columns:
            df['Date Versement Dividende'] = pd.to_datetime(df['Date Versement Dividende'], errors='coerce', dayfirst=True)
        if 'Prochains Résultats' in df.columns:
            df['Prochains Résultats'] = pd.to_datetime(df['Prochains Résultats'], errors='coerce', dayfirst=True)
        ticker_to_name = dict(zip(df['Ticker'], df['Nom']))

        # Colonnes internes à masquer du tableau
        COLS_INTERNES = {'p_details', 'full_data'}

        try:
            df_conf      = get_column_config()
            liste_profils = sorted(df_conf['Profil'].unique().tolist())
            profil_choisi = st.sidebar.selectbox("📋 Vue de tableau", options=liste_profils)
            config_active = df_conf[df_conf['Profil'] == profil_choisi]
            cols_base         = config_active[config_active['Afficher'] == True]['Nom_Colonne'].tolist()
            cols_figees_base  = config_active[config_active['Figer'] == True]['Nom_Colonne'].tolist()
        except Exception as e:
            st.error(f"Erreur configuration colonnes : {e}")
            cols_base, cols_figees_base = ["Ticker", "Nom"], ["Ticker"]

        selection_finale = []
        selection_figee  = []
        config_colonnes  = {col: st.column_config.Column(pinned=True) for col in selection_figee}

        # =======================================================================
        # STYLE DU TABLEAU
        # =======================================================================

        def style_df(df):
            styles = pd.DataFrame('', index=df.index, columns=df.columns)

            if 'Prix Actuel' in df.columns:
                p_actuel = df['Prix Actuel']

                for col in ['Entrée FCF -15%', 'Entrée BNA -15%', 'Entrée Analystes -15%']:
                    if col in df.columns:
                        mask = df[col].fillna(0) > p_actuel
                        styles.loc[mask, col] = 'background-color: #d4edda; color: #155724;'

                if 'Entrée Synthèse (-15%)' in df.columns:
                    styles['Entrée Synthèse (-15%)'] = (
                        'border-left: 2px solid #555; border-right: 2px solid #555; font-weight: bold;'
                    )
                    col_bna = 'Entrée BNA -15%'
                    col_fcf = 'Entrée FCF -15%'
                    if col_bna in df.columns and col_fcf in df.columns:
                        moyenne_entrees = (df[col_bna].fillna(0) + df[col_fcf].fillna(0)) / 2
                        mask_synth = moyenne_entrees > p_actuel
                        styles.loc[mask_synth, 'Entrée Synthèse (-15%)'] += 'background-color: #28a745; color: white;'
                    else:
                        mask_synth = df['Entrée Synthèse (-15%)'] < p_actuel
                        styles.loc[mask_synth, 'Entrée Synthèse (-15%)'] += 'background-color: #28a745; color: white;'

            for col_peg in ['PEG Actuel', 'PEG Forward']:
                if col_peg in df.columns:
                    for i, v in df[col_peg].items():
                        try:
                            val = float(v)
                            if 0 < val < 1:
                                styles.loc[i, col_peg] = 'background-color: #d4edda; color: #155724; font-weight: bold;'
                            elif val > 2:
                                styles.loc[i, col_peg] = 'color: #dc3545; font-weight: bold;'
                        except:
                            pass

            for col_r in ['ROE', 'ROA']:
                if col_r in df.columns:
                    for i, v in df[col_r].items():
                        try:
                            val = float(str(v).replace('%', ''))
                            if val >= 15:
                                styles.loc[i, col_r] = 'color: #28a745; font-weight: bold;'
                            elif val < 0:
                                styles.loc[i, col_r] = 'color: #dc3545; font-weight: bold;'
                        except:
                            pass

            if 'Marge Nette' in df.columns:
                for i, v in df['Marge Nette'].items():
                    try:
                        val = float(str(v).replace('%', ''))
                        if val >= 20:
                            styles.loc[i, 'Marge Nette'] = 'color: #28a745; font-weight: bold;'
                        elif val < 5:
                            styles.loc[i, 'Marge Nette'] = 'color: #dc3545;'
                    except:
                        pass

            for col_cagr in ['CAGR 3 ans', 'CAGR 5 ans']:
                if col_cagr in df.columns:
                    for i, v in df[col_cagr].items():
                        try:
                            val = float(str(v).replace('%', ''))
                            if val >= 10:
                                styles.loc[i, col_cagr] = 'color: #28a745; font-weight: bold;'
                            elif val < 0:
                                styles.loc[i, col_cagr] = 'color: #dc3545; font-weight: bold;'
                        except:
                            pass

            # Coloration P/FCF actuel (< 15x vert, > 30x rouge)
            if 'P/FCF' in df.columns:
                for i, v in df['P/FCF'].items():
                    try:
                        val = float(str(v).replace('x', ''))
                        if val < 15:
                            styles.loc[i, 'P/FCF'] = 'color: #28a745; font-weight: bold;'
                        elif val > 30:
                            styles.loc[i, 'P/FCF'] = 'color: #dc3545; font-weight: bold;'
                    except:
                        pass

            # Coloration P/FCF Moy 3a (< 15x vert, > 30x rouge)
            if 'P/FCF Moy 3a' in df.columns:
                for i, v in df['P/FCF Moy 3a'].items():
                    try:
                        val = float(str(v).replace('x', ''))
                        if val < 15:
                            styles.loc[i, 'P/FCF Moy 3a'] = 'color: #28a745; font-weight: bold;'
                        elif val > 30:
                            styles.loc[i, 'P/FCF Moy 3a'] = 'color: #dc3545; font-weight: bold;'
                    except:
                        pass

            # Coloration Croissance EBITDA (> 10% vert, < 0% rouge)
            if 'Croissance EBITDA' in df.columns:
                for i, v in df['Croissance EBITDA'].items():
                    try:
                        val = float(str(v).replace('%', '').replace('+', ''))
                        if val > 10:
                            styles.loc[i, 'Croissance EBITDA'] = 'color: #28a745; font-weight: bold;'
                        elif val < 0:
                            styles.loc[i, 'Croissance EBITDA'] = 'color: #dc3545; font-weight: bold;'
                    except:
                        pass

            if 'Santé (Piotroski)' in df.columns:
                for i, v in df['Santé (Piotroski)'].items():
                    try:
                        s = int(str(v).split('/')[0])
                        if s >= 4:   styles.loc[i, 'Santé (Piotroski)'] += 'color: #28a745; font-weight: bold;'
                        elif s <= 1: styles.loc[i, 'Santé (Piotroski)'] += 'color: #dc3545; font-weight: bold;'
                    except:
                        pass

            for col in ['Chg 1J', 'Chg 1M', 'Chg YTD']:
                if col in df.columns:
                    mask_plus  = df[col].astype(str).str.contains(r'\+')
                    mask_moins = df[col].astype(str).str.contains('-')
                    styles.loc[mask_plus,  col] += 'color: #28a745; font-weight: bold;'
                    styles.loc[mask_moins, col] += 'color: #dc3545; font-weight: bold;'

            return styles

        # =======================================================================
        # AFFICHAGE
        # =======================================================================

        if show_news_portfolio:
            st.subheader(f"📝 Revue de Presse : {sel_list}")
            if tickers_input:
                liste_tickers = [t.strip().upper() for t in tickers_input.split(',') if t.strip()]
                actualite_module(liste_tickers)
            else:
                st.info("La liste de tickers est vide.")
        else:
            with st.expander("🛠️ Personnaliser les colonnes affichées"):
                # Exclure les colonnes internes de l'interface utilisateur
                toutes_les_cols   = [c for c in df.columns.tolist() if c not in COLS_INTERNES]
                cols_base_filtrees = [c for c in cols_base if c in toutes_les_cols]

                selection_finale = st.multiselect(
                    "Colonnes actives :",
                    options=toutes_les_cols,
                    default=[c for c in cols_base_filtrees if c in toutes_les_cols]
                )
                selection_figee = st.multiselect(
                    "Colonnes à figer à gauche :",
                    options=selection_finale,
                    default=[c for c in cols_figees_base if c in selection_finale]
                )

            hauteur_dynamique = (len(df) * 35) + 38
            sel = st.dataframe(
                df[selection_finale].style.apply(style_df, axis=None).format(
                    formatter=lambda x: clean_num(x) if isinstance(x, (int, float)) else x
                ),
                on_select="rerun",
                selection_mode="single-row",
                width="stretch",
                hide_index=True,
                height=min(hauteur_dynamique, 850),
                column_config={
                    "Date Détachement": st.column_config.DateColumn("Date Détachement", format="DD/MM/YYYY"),
                    "Date Versement Dividende": st.column_config.DateColumn("Date Versement Dividende", format="DD/MM/YYYY"),
                    "Prochains Résultats": st.column_config.DateColumn("Prochains Résultats", format="DD/MM/YYYY"),
                    **{col: st.column_config.Column(pinned=True) for col in selection_figee}
                },
            )

            # =======================================================================
            # VUE DÉTAIL (ligne sélectionnée)
            # =======================================================================

            if sel.selection and sel.selection.rows:
                d = data_res[sel.selection.rows[0]]
                afficher_detail_action(d)