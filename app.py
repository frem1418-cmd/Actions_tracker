import streamlit as st
import yfinance as yf
import pandas as pd
import requests
import feedparser
from datetime import datetime, timedelta
from textblob import TextBlob
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import requests
import time
from bs4 import BeautifulSoup
import streamlit as st
from streamlit_gsheets import GSheetsConnection
from deep_translator import GoogleTranslator
from concurrent.futures import ThreadPoolExecutor
import urllib.parse
import logging
logging.getLogger("streamlit").setLevel(logging.ERROR)

# Initialisation de la connexion (à faire une seule fois)
conn = st.connection("gsheets", type=GSheetsConnection)

@st.cache_data(ttl=900)
def get_bundle_news(liste_tickers, ticker_to_name=None):
    if ticker_to_name is None:
        ticker_to_name = {}
    all_news_combined = []
    
    with ThreadPoolExecutor(max_workers=15) as executor:
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
            except Exception as e:
                print(f"Erreur sur {ticker_parent}: {e}")
                continue
                
    return all_news_combined


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

@st.cache_data(ttl=3600)
def get_column_config():
    return conn.read(worksheet="Choix_colonnes")
    
def update_tickers_callback():
    new_val = st.session_state["ticker_editor"].upper()
    save_watchlist_gsheets(sel_list, new_val)
    st.cache_data.clear()


# --- RÉFÉRENTIELS ---
SECTORS_FR = {
    "Basic Materials": "Matériaux de base", "Communication Services": "Services de communication",
    "Consumer Cyclical": "Consommation cyclique", "Consumer Defensive": "Consommation défensive",
    "Energy": "Énergie", "Financial Services": "Services financiers", "Healthcare": "Santé",
    "Industrials": "Industrie", "Real Estate": "Immobilier", "Technology": "Technologie",
    "Utilities": "Services publics", "Financial": "Finance", "Consumer Discretionary": "Consommation discrétionnaire"
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


# --- FONCTIONS DE CALCUL & UTILITAIRES ---
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
    except: return []

def clean_num(n):
    if isinstance(n, str): return n
    if n is None or pd.isna(n): return "0"
    abs_n = abs(n)
    if abs_n >= 1e12: return f"{n/1e12:.2f} Tn"
    if abs_n >= 1e9: return f"{n/1e9:.2f} Md"
    if abs_n >= 1e6: return f"{n/1e6:.2f} M"
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

        ni_keys = ['Net Income', 'NetIncome', 'Net Income Common Stockholders']
        ocf_keys = ['Operating Cash Flow', 'Total Cash From Operating Activities']
        asset_keys = ['Total Assets', 'TotalAssets']

        ni, ocf, assets = get_val(income, ni_keys, 0), get_val(cash, ocf_keys, 0), get_val(balance, asset_keys, 0)
        ni_p, ocf_p, assets_p = get_val(income, ni_keys, 1), get_val(cash, ocf_keys, 1), get_val(balance, asset_keys, 1)

        if None in [ni, ocf, assets]: return "Incomplet", {}

        roa_n, roa_p = ni/assets, (ni_p/assets_p if assets_p else 0)
        q_n, q_p = ocf - ni, (ocf_p - ni_p if (ni_p is not None and ocf_p is not None) else None)

        checks = {
            "Bénéfice Net": {"status": ni > 0, "detail": f"{clean_num(ni)}", "comparaison": f"N-1: {clean_num(ni_p)} ({get_progression_pct(ni, ni_p):+.1f}%)" if ni_p else "> 0"},
            "Cash Flow Opé.": {"status": ocf > 0, "detail": f"{clean_num(ocf)}", "comparaison": f"N-1: {clean_num(ocf_p)} ({get_progression_pct(ocf, ocf_p):+.1f}%)" if ocf_p else "> 0"},
            "Progression ROA": {"status": roa_n > roa_p, "detail": f"{roa_n:.2%}", "comparaison": f"N-1: {roa_p:.2%} ({get_progression_pct(roa_n, roa_p):+.1f}%)" if roa_p else "N/A"},
            "Qualité Gains": {"status": ocf > ni, "detail": f"Δ {clean_num(q_n)}", "comparaison": f"N-1: Δ {clean_num(q_p)} ({get_progression_pct(q_n, q_p):+.1f}%)" if q_p is not None else "OCF > NI"},
            "Taille Actifs": {"status": assets > (assets_p or 0), "detail": f"{clean_num(assets)}", "comparaison": f"N-1: {clean_num(assets_p)} ({get_progression_pct(assets, assets_p):+.1f}%)" if assets_p else "N/A"}
        }
        return f"{sum(1 for c in checks.values() if c['status'])}/5", checks
    except: return "N/A", {}

@st.cache_data(ttl=3600)
def fetch_stock_data(ticker_str):
    yf.set_tz_cache_location("/tmp")
    try:
        s = yf.Ticker(ticker_str.strip())
        info = s.info
        p = info.get("currentPrice") or info.get("regularMarketPrice")
        if p is None: return None
        ef, pf = info.get("forwardEps", 0), info.get("forwardPE", 15)
        vb = ef * pf
        tm = info.get("targetMeanPrice", 0)
        sh = info.get("sharesOutstanding", 1)
        fcf_raw = s.cashflow.loc["Free Cash Flow"].dropna().head(3).mean() if "Free Cash Flow" in s.cashflow.index else 0
        vf = (fcf_raw/sh * 1.05) * pf if sh > 0 else 0
        mods = [v for v in [vb, vf, tm] if v > 0]
        avg = sum(mods)/len(mods) if mods else 0
        p_s, p_d = calculate_piotroski_advanced(s)

        current_year = datetime.now().year
        hist = s.history(start=f"{current_year}-01-01")
        
        perf_1j, perf_1m, perf_ytd = 0, 0, 0
        
        if len(hist) >= 2:
            c_actuel = p
            c_veille = hist['Close'].iloc[-2]
            perf_1j = ((c_actuel - c_veille) / c_veille) * 100
            c_debut_annee = hist['Close'].iloc[0]
            perf_ytd = ((c_actuel - c_debut_annee) / c_debut_annee) * 100
            if len(hist) >= 20:
                c_debut_mois = hist['Close'].iloc[-20]
                perf_1m = ((c_actuel - c_debut_mois) / c_debut_mois) * 100
            else:
                perf_1m = perf_ytd

        # --- CAGR 3 ans et 5 ans ---
        def calc_cagr(ticker_obj, years):
            try:
                h = ticker_obj.history(period=f"{years}y")
                if len(h) < 20: return None
                c_start = h['Close'].iloc[0]
                c_end = h['Close'].iloc[-1]
                if c_start <= 0: return None
                return ((c_end / c_start) ** (1 / years) - 1) * 100
            except: return None

        cagr_3y = calc_cagr(s, 3)
        cagr_5y = calc_cagr(s, 5)

        def fmt_p(v):
            return f"{v:+.2f}% {'📈' if v > 0 else '📉'}"

        def fmt_pct(v):
            if v is None or pd.isna(v): return "N/A"
            return f"{v:.2f}%"

        curr_raw = info.get('currency', 'EUR')
        sym = "$" if curr_raw == "USD" else "£" if curr_raw == "GBP" else "€"
        
        div_date = info.get("exDividendDate")
        div_date_str = datetime.fromtimestamp(div_date).strftime('%d/%m/%Y') if div_date else "N/A"

        # --- Indicateurs fondamentaux ---
        trailing_eps = info.get("trailingEps", 0) or 0
        trailing_pe  = info.get("trailingPE", 0) or 0

        # PEG actuel = PER actuel / croissance BNA (trailingEps growth)
        eps_growth = info.get("earningsGrowth")  # taux annuel (ex: 0.15 = 15%)
        if eps_growth and eps_growth != 0 and trailing_pe:
            peg_actuel = trailing_pe / (eps_growth * 100)
        else:
            peg_actuel = None

        # PEG forward = PER forward / croissance BNA estimée
        fwd_eps_growth = info.get("earningsQuarterlyGrowth") or info.get("revenueGrowth")
        if fwd_eps_growth and fwd_eps_growth != 0 and pf:
            peg_forward = pf / (fwd_eps_growth * 100)
        else:
            peg_forward = None

        roa = info.get("returnOnAssets")       # ex: 0.12 → 12%
        roe = info.get("returnOnEquity")        # ex: 0.25 → 25%
        marge_nette = info.get("profitMargins") # ex: 0.18 → 18%
        dette_equity = info.get("debtToEquity") # ex: 45.3 → 45.3%
        beta = info.get("beta")

        def pct_fmt(v):
            if v is None or (isinstance(v, float) and pd.isna(v)): return "N/A"
            return f"{v*100:.1f}%"

        def num_fmt(v, decimals=2):
            if v is None or (isinstance(v, float) and pd.isna(v)): return "N/A"
            return f"{v:.{decimals}f}"

        return {
            "Ticker": ticker_str, "Nom": info.get("longName", ticker_str),
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
            "CAGR 3 ans": fmt_pct(cagr_3y),
            "CAGR 5 ans": fmt_pct(cagr_5y),
            "Chg 1J": fmt_p(perf_1j),
            "Chg YTD": fmt_p(perf_ytd),
            "Chg 1M": fmt_p(perf_1m),
            "currency": sym,
            "BNA Forward": ef, "PER Forward": pf, "Nb Analystes": info.get("numberOfAnalystOpinions", 0),
            "Entrée BNA -15%": vb * 0.85, "Entrée FCF -15%": vf * 0.85, "Entrée Analystes -15%": tm * 0.85,
            "Entrée Synthèse (-15%)": avg * 0.85, "Santé (Piotroski)": p_s, "p_details": p_d,
            "Dividende (€/$)": info.get("dividendRate", 0), "Rendement %": round((info.get("dividendRate", 0)/p*100), 2) if info.get("dividendRate") else 0,
            "Date Détachement": div_date_str, "Avis Analystes": RECO_FR.get(info.get("recommendationKey"), "N/A"),
            "full_data": {"val_bna": vb, "val_fcf": vf, "target_mean": tm, "fair_avg": avg, "currency": info.get("currency", "EUR"), "eps_fwd": ef, "per_fwd": pf, "fcf_ps": fcf_raw/sh if sh>0 else 0, "num_analysts": info.get("numberOfAnalystOpinions", 0)}
        }
    except: return None


# --- GESTION LISTES & COLONNES ---
@st.cache_data(ttl=3600)
def get_all_watchlists():
    try:
        conn = st.connection("gsheets", type=GSheetsConnection)
        df = conn.read(worksheet="Watchlists")
        
        watchlists_dict = {}
        if not df.empty and 'list_name' in df.columns:
            col_ticker = 'tickers' if 'tickers' in df.columns else 'Ticker'
            
            for name in df['list_name'].dropna().unique():
                t_data = df[df['list_name'] == name][col_ticker].iloc[0]
                ticker_list = [t.strip().upper() for t in str(t_data).split(',') if t.strip()]
                watchlists_dict[name] = ticker_list
            
            return watchlists_dict
        return {"Actions_EU": ["AAPL"]}
    except:
        return {"Actions_EU": ["AAPL"]}


def delete_watchlist_gsheets(watchlist_name):
    conn = st.connection("gsheets", type=GSheetsConnection)
    df = conn.read(worksheet="Watchlists")
    df_updated = df[df['Wallet_Name'] != watchlist_name]
    conn.update(worksheet="Watchlists", data=df_updated)
    st.cache_data.clear()

def load_watchlist_gsheets(list_name):
    try:
        conn = st.connection("gsheets", type=GSheetsConnection)
        df = conn.read(worksheet="Watchlists")
        res = df[df['list_name'] == list_name]
        if not res.empty:
            return res.iloc[0]['tickers']
        return ""
    except Exception as e:
        return ""

def save_watchlist_gsheets(list_name, tickers_text):
    try:
        conn = st.connection("gsheets", type=GSheetsConnection)
        df = conn.read(worksheet="Watchlists")
        
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


# --- INTERFACE ---
st.set_page_config(page_title="Analyseur Pro+", layout="wide")
st.markdown(
    """
    <style>
    .block-container {
        padding-top: 3.5rem !important;
    }
    [data-testid="stSidebarNav"] {padding-top: 0rem;}
    [data-testid="stSidebarContent"] > div:first-child {padding-top: 1rem;}
    [data-testid='stTable'] {font-size: 13px;}
    .stVerticalBlock {gap: 0.5rem;}
    </style>
    """,
    unsafe_allow_html=True
)

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
            opt = [x['label'] for x in sug]
            sel_opt = st.selectbox("Résultats :", opt)
            tk_add = sug[opt.index(sel_opt)]['symbol']
            if st.button(f"➕ Ajouter {tk_add}"):
                cur_tk = load_watchlist_gsheets(st.session_state.get('sel_list', 'Portefeuille Principal'))
                new_tickers_list = cur_tk + f", {tk_add}"
                save_watchlist_gsheets(st.session_state.get('sel_list', 'Portefeuille Principal'), new_tickers_list)
                st.session_state["ticker_editor"] = new_tickers_list
                st.cache_data.clear()
                st.rerun()

    st.divider()
    
    st.header("📂 Portefeuilles")
    lists = get_all_watchlists()
    sel_list = st.selectbox(
        "Liste active :", 
        options=list(lists.keys()), 
        key='sel_list', 
        on_change=on_list_change
    )

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
                import time
                time.sleep(0.5)
                st.rerun()
            else:
                st.error("Nom vide !")

    if show_del:
        st.warning(f"⚠️ Action irréversible")
        list_to_del = st.selectbox("Choisir la liste à supprimer :", lists, key="del_select_box")
        
        if st.button(f"Confirmer la suppression de {list_to_del}", type="primary", key="btn_confirm_del"):
            if len(lists) > 1:
                try:
                    conn = st.connection("gsheets", type=GSheetsConnection)
                    df_all = conn.read(worksheet="Watchlists")
                    df_updated = df_all[df_all['list_name'] != list_to_del]
                    conn.update(worksheet="Watchlists", data=df_updated)
                    st.success(f"🔥 Liste '{list_to_del}' supprimée avec succès !")
                    st.cache_data.clear()
                    import time
                    time.sleep(0.5)
                    st.rerun()
                    
                except Exception as e:
                    st.error(f"Erreur lors de la suppression : {e}")
            else:
                st.error("🚫 Impossible de supprimer la dernière liste !")

    st.sidebar.markdown("<br>", unsafe_allow_html=True) 

    col_news1, col_news2 = st.sidebar.columns([0.5, 4], vertical_alignment="center")

    with col_news1:
        show_news_portfolio = st.checkbox(
            "Actualités", 
            value=False, 
            key="chk_news_port",
            label_visibility="collapsed"
        )

    with col_news2:
        st.markdown("📰 **Actualités**", help="Afficher les Actualités du portefeuille")
    
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
    cols_all = ["Nom", "Secteur", "Prix Actuel", "BNA Actuel", "PER Actuel", "PEG Actuel", "PEG Forward",
                "BNA Forward", "PER Forward", "ROA", "ROE", "Marge Nette", "Dette/Equity", "Beta",
                "CAGR 3 ans", "CAGR 5 ans",
                "Entrée BNA -15%", "Entrée FCF -15%", "Entrée Analystes -15%", "Entrée Synthèse (-15%)", 
                "Santé (Piotroski)", "Chg 1J", "Chg 1M", "Chg YTD", "Nb Analystes", "Dividende (€/$)", "Rendement %", "Date Détachement", "Avis Analystes"]

st.title(f"📈 {sel_list}")
t_list = [t.strip().upper() for t in tickers_input.replace('\r', '').replace('\n', ',').split(',') if t.strip()]


@st.cache_data(ttl=3600)
def get_column_config():
    return conn.read(worksheet="Choix_colonnes")


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
        ticker_to_name = dict(zip(df['Ticker'], df['Nom']))

        try:
            df_conf = get_column_config()
            liste_profils = sorted(df_conf['Profil'].unique().tolist())
            profil_choisi = st.sidebar.selectbox("📋 Vue de tableau", options=liste_profils)
            config_active = df_conf[df_conf['Profil'] == profil_choisi]            
            cols_base = config_active[config_active['Afficher'] == True]['Nom_Colonne'].tolist()
            cols_figees_base = config_active[config_active['Figer'] == True]['Nom_Colonne'].tolist()

        except Exception as e:
            st.error(f"Erreur configuration colonnes : {e}")
            cols_base, cols_figees_base = ["Ticker", "Nom"], ["Ticker"]

        selection_finale = []
        selection_figee = []
        config_colonnes = {col: st.column_config.Column(pinned=True) for col in selection_figee}

    
        def style_df(df):
            styles = pd.DataFrame('', index=df.index, columns=df.columns)
            
            if 'Prix Actuel' in df.columns:
                p_actuel = df['Prix Actuel']
                
                # Entrées individuelles (Vert si > Prix)
                for col in ['Entrée FCF -15%', 'Entrée BNA -15%', 'Entrée Analystes -15%']:
                    if col in df.columns:
                        mask = df[col].fillna(0) > p_actuel
                        styles.loc[mask, col] = 'background-color: #d4edda; color: #155724;'

                # -------------------------------------------------------
                # ENTRÉE SYNTHÈSE : Vert SEULEMENT si la moyenne
                # (BNA -15% + FCF -15%) / 2  est INFÉRIEURE au Prix Actuel
                # = les deux modèles fondamentaux estiment un point d'entrée
                #   sous le cours actuel → signal d'achat cohérent
                # -------------------------------------------------------
                if 'Entrée Synthèse (-15%)' in df.columns:
                    # Style de base : bordure encadrante + gras
                    styles['Entrée Synthèse (-15%)'] = (
                        'border-left: 2px solid #555; '
                        'border-right: 2px solid #555; '
                        'font-weight: bold;'
                    )

                    col_bna = 'Entrée BNA -15%'
                    col_fcf = 'Entrée FCF -15%'

                    if col_bna in df.columns and col_fcf in df.columns:
                        # Moyenne des deux points d'entrée modèles
                        moyenne_entrees = (df[col_bna].fillna(0) + df[col_fcf].fillna(0)) / 2
                        # Signal achat = moyenne < prix actuel
                        mask_synth = moyenne_entrees < p_actuel
                        styles.loc[mask_synth, 'Entrée Synthèse (-15%)'] += (
                            'background-color: #28a745; color: white;'
                        )
                    else:
                        # Fallback si une colonne source manque
                        mask_synth = df['Entrée Synthèse (-15%)'] > p_actuel
                        styles.loc[mask_synth, 'Entrée Synthèse (-15%)'] += (
                            'background-color: #28a745; color: white;'
                        )

            # --- Coloration PEG (< 1 = sous-évalué vert, > 2 = rouge) ---
            for col_peg in ['PEG Actuel', 'PEG Forward']:
                if col_peg in df.columns:
                    for i, v in df[col_peg].items():
                        try:
                            val = float(v)
                            if 0 < val < 1:
                                styles.loc[i, col_peg] = 'background-color: #d4edda; color: #155724; font-weight: bold;'
                            elif val > 2:
                                styles.loc[i, col_peg] = 'color: #dc3545; font-weight: bold;'
                        except: pass

            # --- Coloration ROE / ROA (> 15% vert, < 0 rouge) ---
            for col_r in ['ROE', 'ROA']:
                if col_r in df.columns:
                    for i, v in df[col_r].items():
                        try:
                            val = float(str(v).replace('%', ''))
                            if val >= 15:
                                styles.loc[i, col_r] = 'color: #28a745; font-weight: bold;'
                            elif val < 0:
                                styles.loc[i, col_r] = 'color: #dc3545; font-weight: bold;'
                        except: pass

            # --- Coloration Marge Nette (> 20% vert, < 5% rouge) ---
            if 'Marge Nette' in df.columns:
                for i, v in df['Marge Nette'].items():
                    try:
                        val = float(str(v).replace('%', ''))
                        if val >= 20:
                            styles.loc[i, 'Marge Nette'] = 'color: #28a745; font-weight: bold;'
                        elif val < 5:
                            styles.loc[i, 'Marge Nette'] = 'color: #dc3545;'
                    except: pass

            # --- Coloration CAGR ---
            for col_cagr in ['CAGR 3 ans', 'CAGR 5 ans']:
                if col_cagr in df.columns:
                    for i, v in df[col_cagr].items():
                        try:
                            val = float(str(v).replace('%', ''))
                            if val >= 10:
                                styles.loc[i, col_cagr] = 'color: #28a745; font-weight: bold;'
                            elif val < 0:
                                styles.loc[i, col_cagr] = 'color: #dc3545; font-weight: bold;'
                        except: pass

            # Coloration Piotroski
            if 'Santé (Piotroski)' in df.columns:
                for i, v in df['Santé (Piotroski)'].items():
                    try:
                        s = int(str(v).split('/')[0])
                        if s >= 4: styles.loc[i, 'Santé (Piotroski)'] += 'color: #28a745; font-weight: bold;'
                        elif s <= 1: styles.loc[i, 'Santé (Piotroski)'] += 'color: #dc3545; font-weight: bold;'
                    except: pass

            # Coloration des performances
            for col in ['Chg 1J', 'Chg 1M', 'Chg YTD']:
                if col in df.columns:
                    mask_plus = df[col].astype(str).str.contains(r'\+')
                    mask_moins = df[col].astype(str).str.contains('-')
                    styles.loc[mask_plus, col] += 'color: #28a745; font-weight: bold;'
                    styles.loc[mask_moins, col] += 'color: #dc3545; font-weight: bold;'

            return styles


        if show_news_portfolio:
            st.subheader(f"📝 Revue de Presse : {sel_list}")

            if tickers_input:
                liste_tickers = [t.strip().upper() for t in tickers_input.split(',') if t.strip()]
                actualite_module(liste_tickers)
            else:
                st.info("La liste de tickers est vide.")
        else:
            with st.expander("🛠️ Personnaliser les colonnes affichées"):
                toutes_les_cols = df.columns.tolist()
                
                selection_finale = st.multiselect(
                    "Colonnes actives :",
                    options=toutes_les_cols,
                    default=[c for c in cols_base if c in toutes_les_cols]
                )
                
                selection_figee = st.multiselect(
                    "Colonnes à figer à gauche :",
                    options=selection_finale,
                    default=[c for c in cols_figees_base if c in selection_finale]
                )

            hauteur_dynamique = (len(df) * 35) + 38
            sel = st.dataframe(
                df[selection_finale].style.apply(style_df, axis=None).format(formatter=lambda x: clean_num(x) if isinstance(x, (int, float)) else x),
                on_select="rerun",
                selection_mode="single-row",
                width="stretch",
                hide_index=True,
                height=min(hauteur_dynamique, 850),
                column_config={
                    "Date Détachement": st.column_config.DateColumn(
                        "Date Détachement",
                        format="DD/MM/YYYY",
                    ),
                    **config_colonnes 
                },
            )

            if sel.selection and sel.selection.rows:
                d = data_res[sel.selection.rows[0]]
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
                    # SECTION GRAPHIQUE AVANCÉ AVEC INDICATEURS TECHNIQUES
                    # ===================================================
                    st.divider()
                    st.subheader(f"📈 Performance & Volumes")

                    try:
                        s_obj = yf.Ticker(d['Ticker'])
                        current_yr = datetime.now().year

                        # --- CONTRÔLES ---
                        ctrl_col1, ctrl_col2 = st.columns([0.45, 0.55])
                        with ctrl_col1:
                            st.caption("📅 Période")
                            periode_choisie = st.radio(
                                "Période",                        # ← label non vide (évite "undefined")
                                ["YTD", "1 an", "2 ans", "5 ans", "Max"],
                                horizontal=True,
                                key=f"periode_{d['Ticker']}",
                                label_visibility="collapsed"      # masqué visuellement
                            )
                        with ctrl_col2:
                            st.caption("📊 Indicateur")
                            indicateur_choisi = st.radio(
                                "Indicateur",                     # ← label non vide
                                ["RSI", "MACD", "Bollinger + MA"],
                                index=0,                          # ← RSI sélectionné par défaut
                                horizontal=True,
                                key=f"indic_{d['Ticker']}",
                                label_visibility="collapsed"
                            )

                        # --- DÉTERMINATION DE LA DATE DE DÉBUT ---
                        today = datetime.now()
                        warmup = 250  # jours pour stabiliser MA200
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
                        else:  # Max
                            date_calcul    = "1985-01-01"
                            date_affichage = "1985-01-01"

                        h_data_large = s_obj.history(start=date_calcul)

                        if not h_data_large.empty:

                            # ---- CALCUL INDICATEURS ----
                            h_data_large['MA20']  = h_data_large['Close'].rolling(20).mean()
                            h_data_large['MA50']  = h_data_large['Close'].rolling(50).mean()
                            h_data_large['MA100'] = h_data_large['Close'].rolling(100).mean()
                            h_data_large['MA200'] = h_data_large['Close'].rolling(200).mean()

                            # Bollinger ±2σ
                            h_data_large['BB_std']   = h_data_large['Close'].rolling(20).std()
                            h_data_large['BB_upper'] = h_data_large['MA20'] + h_data_large['BB_std'] * 2
                            h_data_large['BB_lower'] = h_data_large['MA20'] - h_data_large['BB_std'] * 2

                            # RSI 14
                            delta_c = h_data_large['Close'].diff()
                            gain_c  = delta_c.clip(lower=0).rolling(14).mean()
                            loss_c  = (-delta_c.clip(upper=0)).rolling(14).mean()
                            rs_c    = gain_c / loss_c.replace(0, float('nan'))
                            h_data_large['RSI'] = 100 - (100 / (1 + rs_c))

                            # MACD (12, 26, 9)
                            ema12 = h_data_large['Close'].ewm(span=12, adjust=False).mean()
                            ema26 = h_data_large['Close'].ewm(span=26, adjust=False).mean()
                            h_data_large['MACD']        = ema12 - ema26
                            h_data_large['MACD_signal'] = h_data_large['MACD'].ewm(span=9, adjust=False).mean()
                            h_data_large['MACD_hist']   = h_data_large['MACD'] - h_data_large['MACD_signal']

                            # PER historique (proxy BNA constant)
                            bna_actuel = d.get('BNA Actuel', 0)
                            if bna_actuel and bna_actuel > 0:
                                h_data_large['PER_hist'] = h_data_large['Close'] / bna_actuel

                            # Filtre période d'affichage
                            h_data = h_data_large[h_data_large.index >= date_affichage].copy()

                            # Couleurs volumes
                            colors_vol = [
                                '#28a745' if row['Close'] >= row['Open'] else '#dc3545'
                                for _, row in h_data.iterrows()
                            ]

                            # ================================================================
                            # SUBPLOTS : 3 rangées
                            #   row 1 : Cours (axe DROIT = prix) + Volume (axe GAUCHE)
                            #   row 2 : Indicateur choisi
                            #   row 3 : PER historique
                            # ================================================================
                            fig = make_subplots(
                                rows=3, cols=1,
                                shared_xaxes=True,
                                row_heights=[0.55, 0.25, 0.20],
                                vertical_spacing=0.03,
                                specs=[
                                    [{"secondary_y": True}],   # prix (droite) + volume (gauche)
                                    [{"secondary_y": False}],  # indicateur
                                    [{"secondary_y": False}],  # PER
                                ]
                            )

                            # ---- RANGÉE 1 : Volume (axe GAUCHE, secondary_y=False) ----
                            fig.add_trace(go.Bar(
                                x=h_data.index, y=h_data['Volume'],
                                name="Volume", marker_color=colors_vol, opacity=0.30
                            ), row=1, col=1, secondary_y=False)

                            # ---- RANGÉE 1 : Prix (axe DROIT, secondary_y=True) ----
                            fig.add_trace(go.Scatter(
                                x=h_data.index, y=h_data['Close'],
                                name="Prix", line=dict(color='#1a73e8', width=2)
                            ), row=1, col=1, secondary_y=True)

                            # MA50 (toujours)
                            fig.add_trace(go.Scatter(
                                x=h_data.index, y=h_data['MA50'],
                                name="MA50", line=dict(color='orange', dash='dot', width=1.5)
                            ), row=1, col=1, secondary_y=True)

                            # MA100 (si assez de données)
                            if h_data['MA100'].notna().sum() > 10:
                                fig.add_trace(go.Scatter(
                                    x=h_data.index, y=h_data['MA100'],
                                    name="MA100", line=dict(color='#00bcd4', dash='dot', width=1.5)
                                ), row=1, col=1, secondary_y=True)

                            # MA200 (si assez de données)
                            if h_data['MA200'].notna().sum() > 10:
                                fig.add_trace(go.Scatter(
                                    x=h_data.index, y=h_data['MA200'],
                                    name="MA200", line=dict(color='#e91e63', dash='dot', width=1.5)
                                ), row=1, col=1, secondary_y=True)

                            # Bollinger sur le graphique cours si mode sélectionné
                            if indicateur_choisi == "Bollinger + MA":
                                fig.add_trace(go.Scatter(
                                    x=h_data.index, y=h_data['BB_upper'],
                                    name="BB Sup", line=dict(color='rgba(100,100,200,0.5)', width=1),
                                    fill=None
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

                            # Ligne prix actuel & zone achat — on référence "y2" = secondary_y de row 1
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

                            # ---- RANGÉE 2 : Indicateur choisi ----
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

                            # ---- RANGÉE 3 : PER historique ----
                            if 'PER_hist' in h_data.columns and h_data['PER_hist'].notna().sum() > 5:
                                per_colors = [
                                    '#dc3545' if v > 30 else '#28a745' if v < 15 else '#1a73e8'
                                    for v in h_data['PER_hist'].fillna(0)
                                ]
                                fig.add_trace(go.Scatter(
                                    x=h_data.index, y=h_data['PER_hist'],
                                    name="PER historique",
                                    line=dict(color='#ff9800', width=1.5),
                                    fill='tozeroy', fillcolor='rgba(255,152,0,0.08)'
                                ), row=3, col=1)
                                for per_ref, per_col, per_lbl in [
                                    (15, "rgba(40,167,69,0.6)",  "PER 15"),
                                    (20, "rgba(100,100,200,0.5)", "PER 20"),
                                    (30, "rgba(220,53,69,0.6)",  "PER 30"),
                                ]:
                                    fig.add_hline(
                                        y=per_ref, line_color=per_col, line_dash="dot",
                                        annotation_text=per_lbl, annotation_position="right",
                                        row=3, col=1
                                    )
                                fig.update_yaxes(title_text="PER", row=3, col=1)
                            else:
                                fig.add_annotation(
                                    xref="paper", yref="paper", x=0.5, y=0.04,
                                    text="PER non disponible (BNA = 0 ou négatif)",
                                    showarrow=False, font=dict(color="gray", size=10)
                                )

                            # ---- MISE EN FORME GLOBALE ----
                            fig.update_layout(
                                title=None,
                                height=700,
                                margin=dict(l=10, r=70, t=10, b=10),
                                hovermode="x unified",
                                template="plotly_white",
                                legend=dict(
                                    orientation="h",
                                    yanchor="top", y=-0.05,
                                    xanchor="center", x=0.5,
                                    font=dict(size=11)
                                )
                            )

                            # Axe GAUCHE row 1 = Volume
                            fig.update_yaxes(
                                title_text="Volume",
                                secondary_y=False, row=1, col=1,
                                showgrid=False, fixedrange=False,
                                tickformat=".2s",
                                side="left"
                            )
                            # Axe DROIT row 1 = Prix
                            fig.update_yaxes(
                                title_text="Prix",
                                secondary_y=True, row=1, col=1,
                                showgrid=True, gridcolor='rgba(200,200,200,0.4)',
                                fixedrange=False,
                                side="right"
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
                        ("2️⃣ Modèle FCF (Moyen)", fd['val_fcf'], f"(FCF/Action {clean_num(fd['fcf_ps'])}) × 1.05 × PER Fwd"),
                        ("3️⃣ Analystes", fd['target_mean'], f"Moyenne de {fd['num_analysts']} opinions")
                    ]
                    for title, val, formula in v_configs:
                        if val > 0:
                            with st.expander(f"{title} : {clean_num(val)} {fd['currency']}", expanded=True):
                                st.caption(f"Calcul : {formula}")
                                m1, m2, m3, m4 = st.columns(4)
                                m1.metric("Juste Prix", clean_num(val))
                                m2.metric("-10%", clean_num(val*0.9))
                                m3.metric("-12%", clean_num(val*0.88))
                                m4.metric("-15%", clean_num(val*0.85))

                with c2:
                    st.metric("Prix Actuel", f"{clean_num(d['Prix Actuel'])} {fd['currency']}")
                    st.markdown(f"<div style='background:#28a745; color:white; padding:25px; border-radius:15px; text-align:center;'><small>ENTRÉE CONSEILLÉE (-15%)</small><br/><span style='font-size:36px; font-weight:bold;'>{clean_num(fd['fair_avg']*0.85)}</span></div>", unsafe_allow_html=True)
                    st.divider()
                    st.write(f"**Dividende :** {clean_num(d['Dividende (€/$)'])} {fd['currency']} ({d['Rendement %']}%)")
                    st.write(f"**Détachement :** {d['Date Détachement']}")
                    st.write(f"**Avis :** {d['Avis Analystes']} | **Secteur :** {d['Secteur']}")

                    mode_fr = False
                    ticker_clean = "AAPL"
                    if d and 'Ticker' in d:
                        ticker_clean = str(d['Ticker']).split('.')[0].upper()
                        nom_action_vue = d.get('Nom', ticker_clean)
                    else:
                        ticker_clean = "AAPL"
                        nom_action_vue = "Apple"

                    st.divider()
                    col_titre, col_switch = st.columns([3, 1])
                    
                    with col_titre:
                        st.markdown(f"### 📰 Dernières Actualités : {nom_action_vue}")
                    with col_switch:
                        mode_fr = st.toggle("FR", help="Traduction automatique des titres en français", value=mode_fr)

                    all_news = get_quick_news(ticker_clean)

                    if all_news:
                        all_news.sort(key=lambda x: x.get('dt_obj', datetime.now()), reverse=True)
                        
                        unique_news = []
                        titres_vus = set()
                        
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
                        lien_reel = article.get('lien', '#') 
                        source = article.get('source', 'Info').strip('() ')
                        date = article.get('date', 'Auj.')
                        badge = article.get('badge', '🌐')
                        titre_brut = article.get('titre', 'Sans titre')
                        is_seeking = "seekingalpha.com" in lien_reel.lower()
                        mots_en = {'the', 'stock', 'growth', 'fed', 'market', 'earnings'}
                        est_anglais = any(w in titre_brut.lower() for w in mots_en) or "seekingalpha" in lien_reel.lower()
                        
                        if mode_fr and est_anglais:
                            titre_affiche = safe_translate(titre_brut)
                        else:
                            titre_affiche = titre_brut

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
