import streamlit as st
import yfinance as yf
import pandas as pd
import os
import requests
import feedparser
from datetime import datetime, timedelta
from textblob import TextBlob
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import requests
from bs4 import BeautifulSoup
import streamlit as st
from streamlit_gsheets import GSheetsConnection

# Initialisation de la connexion (à faire une seule fois)
conn = st.connection("gsheets", type=GSheetsConnection)

#Fonction pour récupérer les news et analyser le sentiment
def get_quick_news(ticker):
    news_list = []
    t_clean = ticker.split('.')[0].upper()
    # On récupère la date du jour au format Google pour comparer
    # ex: "21 Apr"
    today_str = datetime.now().strftime("%d %b")
    # --- 1. Google News FR ---
    try:
        url = f"https://news.google.com/rss/search?q={t_clean}+bourse&hl=fr&gl=FR&ceid=FR:fr"
        f = feedparser.parse(url)
        for e in f.entries[:5]:
            pol = TextBlob(e.title).sentiment.polarity
            icon = "🟢" if pol > 0.1 else "🔴" if pol < -0.1 else "⚪"
            raw_pub = e.published
            day_month = raw_pub[5:11] # "21 Apr"
            hour = raw_pub[17:22]    # "10:30"
            
            # Si c'est aujourd'hui, on remplace par Today
            display_date = f"Today {hour}" if day_month == today_str else f"{day_month} {hour}"
            
            # On stocke aussi l'objet datetime pour le tri final
            dt_obj = datetime.strptime(raw_pub[5:25], "%d %b %Y %H:%M:%S")

            news_list.append({
                'dt_obj': dt_obj,
                'date': display_date,
                'titre': e.title, 'lien': e.link, 'badge': f"{icon} 🌐"
            })
    except: pass

    # --- 2. Finviz US ---
    try:
        session = requests.Session()
        h = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }
        r = requests.get(f"https://finviz.com/quote.ashx?t={t_clean}", headers=h, timeout=5)
        if r.status_code == 200:
            soup = BeautifulSoup(r.content, 'html.parser')
            table = soup.find(id='news-table')
            if table:
                last_dt_raw = ""
                for row in table.findAll('tr')[:5]:
                    tds = row.findAll('td')
                    raw_dt = tds[0].get_text(strip=True)
                    
                    if " " in raw_dt:
                        date_part = raw_dt.split(' ')[0] 
                        tm_12h = raw_dt.split(' ')[1] # ex: "10:00PM"
                        last_dt_raw = date_part
                    else:
                        tm_12h = raw_dt
                        date_part = last_dt_raw
                    
                    # Conversion de l'heure AM/PM en 24h
                    # On crée un objet time pour transformer 10:00PM en 22:00
                    time_obj = datetime.strptime(tm_12h, "%I:%M%p")
                    hour_24 = time_obj.strftime("%H:%M")

                    if date_part == "Today":
                        dt_obj = datetime.combine(datetime.now().date(), time_obj.time())
                        display_date = f"Today {hour_24}"
                    else:
                        parts = date_part.split("-") # "Apr-20-26"
                        display_date = f"{parts[1]} {parts[0]} {hour_24}"
                        dt_obj = datetime.strptime(f"{date_part} {tm_12h}", "%b-%d-%y %I:%M%p")

                    t_text = row.a.get_text()
                    icon = "🟢" if TextBlob(t_text).sentiment.polarity > 0.1 else "⚪"
                    
                    news_list.append({
                        'dt_obj': dt_obj, 'date': display_date,
                        'titre': t_text, 'lien': row.a['href'], 'badge': f"{icon} 📈"
                    })
    except: pass
    # --- LE TRI FINAL (Plus récent en haut) ---
    # On trie la liste par l'objet 'dt_obj' du plus récent au plus ancien
    news_list.sort(key=lambda x: x['dt_obj'], reverse=True)

    return news_list
# ---  FONCTIONS de rafraichissement de news ---
@st.cache_data(ttl=86400) # Cache le nom 24h
def get_action_name(ticker):
    try:
        return yf.Ticker(ticker).info.get('longName', ticker)
    except:
        return ticker
    
@st.fragment(run_every="5m") # S'actualise seul toutes les 5 minutes
def news_dashboard_module(liste_tickers):
    # Barre d'outils discrète en haut du module
    
    col1, col2 = st.columns([0.8, 0.2])
    with col1:
        st.subheader("🗞️ Flux d'actualités en direct")
    with col2:
        if st.button("🔄 Actualiser", key="ref_action"):
            st.rerun(scope="fragment")

    for t in liste_tickers:
        nom_action = get_action_name(t)
        
        with st.expander(f"🏢 **{nom_action}** ({t})", expanded=True):
            articles = get_quick_news(t) 
            if articles:
                for a in articles:
                    # Rappel du format : Sentiment/Source | Date 24h | Titre
                    st.markdown(f"{a['badge']} | **{a['date']}** | [{a['titre']}]({a['lien']})")
            else:
                st.caption(f"Aucune actualité récente pour {t}.")    


# --- Fonction New - Vue Flux ou Timeline- liste chronologique des news ---
@st.fragment(run_every="5m")
def news_timeline_module(liste_tickers):
    col_titre, col_btn = st.columns([0.8, 0.2])
    with col_btn:
        if st.button("🔄 Actualiser", key="ref_flux"):
            st.rerun(scope="fragment")
    # --------------------------
    
    all_news = []
        
    # 1. On collecte toutes les news de tous les tickers
    for t in liste_tickers:
        nom = get_action_name(t)
        articles = get_quick_news(t)
        for a in articles:
            # On ajoute le nom de l'action au titre pour savoir de qui on parle
            a['display_title'] = f"**{nom}** ({t}) : : {a['titre']}"
            all_news.append(a)
    
    # 2. On trie tout par date (la plus récente en haut)
    all_news.sort(key=lambda x: x['dt_obj'], reverse=True)
    
    # 3. Affichage
    st.write("---")
    if all_news:
        for a in all_news[:50]: # On affiche les 50 plus récentes
            st.markdown(f"{a['badge']} | {a['date']} | [{a['display_title']}]({a['lien']})")
    else:
        st.info("Aucune news disponible.")    

# --- FONCTION DE CHARGEMENT DES WATCHLISTS DEPUIS GOOGLE SHEETS ---
def load_watchlist_sheets(watchlist_name):
    try:
        conn = st.connection("gsheets", type=GSheetsConnection)
        df = conn.read(worksheet="Watchlists") # Nom de ton onglet Google Sheets
        # On cherche la ligne qui correspond au nom de la liste
        row = df[df['list_name'] == watchlist_name]
        if not row.empty:
            return row.iloc[0]['tickers']
        return ""
    except Exception as e:
        st.error(f"Erreur Sheets: {e}")
        return ""
    
# --- FONCTION DE SAUVEGARDE DES WATCHLISTS VERS GOOGLE SHEETS ---    
def update_tickers_callback():
    # On récupère le texte saisi dans le text_area via sa clé
    new_val = st.session_state["ticker_editor"].upper()
    # On sauvegarde dans GSheets
    save_watchlist_gsheets(sel_list, new_val)
    # On vide le cache pour que le tableau se mette à jour avec les nouveaux cours
    st.cache_data.clear()
# --- 1. CONFIGURATION & DOSSIERS ---
COLUMNS_FILE = "columns_config.txt"

# --- 2. RÉFÉRENTIELS ---
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


# --- 3. FONCTIONS DE CALCUL & UTILITAIRES ---
def search_ticker(query):
    try:
        if not query: return []
        url = f"https://query2.finance.yahoo.com/v1/finance/search?q={query}"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers)
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

@st.cache_data(ttl=600)
def fetch_stock_data(ticker_str):
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
       # 1. On récupère d'abord l'historique YTD (qui contient aussi le mois et la veille)
        current_year = datetime.now().year
        hist = s.history(start=f"{current_year}-01-01")
        
        perf_1j, perf_1m, perf_ytd = 0, 0, 0
        
        if len(hist) >= 2:
            c_actuel = p
            # Calcul 1 jour
            c_veille = hist['Close'].iloc[-2]
            perf_1j = ((c_actuel - c_veille) / c_veille) * 100
            
            # Calcul YTD (début d'année)
            c_debut_annee = hist['Close'].iloc[0]
            perf_ytd = ((c_actuel - c_debut_annee) / c_debut_annee) * 100
            
            # Calcul 1 mois (si on a au moins 20 jours de bourse)
            if len(hist) >= 20:
                c_debut_mois = hist['Close'].iloc[-20]
                perf_1m = ((c_actuel - c_debut_mois) / c_debut_mois) * 100
            else:
                # Si l'année vient de commencer (ex: en janvier), 1M = YTD
                perf_1m = perf_ytd

        # 2. Une seule fonction de formatage
        def fmt_p(v):
            return f"{v:+.2f}% {'📈' if v > 0 else '📉'}"
            
        # 3. Extraction de la devise (indispensable pour ton PDF et tes metrics)
        curr_raw = info.get('currency', 'EUR')
        sym = "$" if curr_raw == "USD" else "£" if curr_raw == "GBP" else "€"
        
        div_date = info.get("exDividendDate")
        div_date_str = datetime.fromtimestamp(div_date).strftime('%d/%m/%Y') if div_date else "N/A"
        
        return {
            "Ticker": ticker_str, "Nom": info.get("longName", ticker_str),
            "Secteur": SECTORS_FR.get(info.get("sector"), info.get("sector")),
            "Prix Actuel": p, "BNA Actuel": info.get("trailingEps", 0), "PER Actuel": info.get("trailingPE", 0),
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

# --- 4. GESTION LISTES & COLONNES ---
def get_all_watchlists():
    try:
        conn = st.connection("gsheets", type=GSheetsConnection)
        df = conn.read(worksheet="Watchlists")
        # On récupère les noms uniques dans la colonne 'list_name'
        if 'list_name' in df.columns:
            return sorted(df['list_name'].dropna().unique().tolist())
        return ["Portefeuille Principal"]
    except Exception as e:
        # En cas d'erreur (ex: pas de connexion), on renvoie une liste par défaut
        return ["Portefeuille Principal"]


def delete_watchlist_gsheets(watchlist_name):
    # 1. Connexion à Google Sheets
    conn = st.connection("gsheets", type=GSheetsConnection)
    
    # 2. Lecture des données actuelles
    df = conn.read(worksheet="Watchlists")
    
    # 3. On garde toutes les lignes SAUF celle qu'on veut supprimer
    df_updated = df[df['Wallet_Name'] != watchlist_name]
    
    # 4. On écrase le Sheets avec le nouveau DataFrame
    conn.update(worksheet="Watchlists", data=df_updated)
    
    # 5. On vide le cache pour que la liste disparaisse du menu
    st.cache_data.clear()

def load_columns(all_cols):
    if os.path.exists(COLUMNS_FILE):
        try:
            # On force l'encodage ET on gère les erreurs de lecture
            with open(COLUMNS_FILE, "r", encoding="utf-8") as f:
                saved = f.read().split(",")
                return [c for c in saved if c in all_cols]
        except Exception:
            # Si le fichier est illisible, on ne plante pas, on renvoie les défauts
            default_cols = ["Nom", "Secteur", "Prix Actuel", "Entrée Synthèse (-15%)", "Entrée BNA -15%", "Entrée FCF -15%", "Entrée Analystes -15%", "Avis Analystes", "Nb Analystes" "Santé (Piotroski)"]
            return default_cols
    return ["Nom", "Secteur", "Prix Actuel", "Entrée Synthèse (-15%)", "Avis Analystes"]

# --- FONCTION DE CHARGEMENT DES WATCHLISTS DEPUIS GOOGLE SHEETS ---
def load_watchlist_gsheets(list_name):
    try:
        conn = st.connection("gsheets", type=GSheetsConnection)
        # On lit l'onglet nommé "Watchlists"
        df = conn.read(worksheet="Watchlists")
        
        # On filtre pour trouver la bonne liste
        res = df[df['list_name'] == list_name]
        if not res.empty:
            return res.iloc[0]['tickers']
        return ""
    except Exception as e:
        # Si ça rate, on ne bloque pas tout, on renvoie vide
        return ""
# --- FONCTION DE SAUVEGARDE DES WATCHLISTS VERS GOOGLE SHEETS ---
def save_watchlist_gsheets(list_name, tickers_text):
    try:
        conn = st.connection("gsheets", type=GSheetsConnection)
        df = conn.read(worksheet="Watchlists")
        
        # Si la liste existe, on met à jour, sinon on ajoute une ligne
        if list_name in df['list_name'].values:
            df.loc[df['list_name'] == list_name, 'tickers'] = tickers_text
        else:
            new_row = pd.DataFrame({'list_name': [list_name], 'tickers': [tickers_text]})
            df = pd.concat([df, new_row], ignore_index=True)
            
        conn.update(worksheet="Watchlists", data=df)
        st.success(f"✅ Liste '{list_name}' synchronisée !")
    except Exception as e:
        st.error(f"Erreur de sauvegarde : {e}")

# --- FONCTION DE GESTION DE LA MISE À JOUR APRÈS ÉDITION MANUELLE ---
def on_list_change():
    # On vide le cache GSheets
    st.cache_data.clear()
    # On force le nettoyage de la mémoire de l'éditeur
    if "ticker_editor" in st.session_state:
        st.session_state.ticker_editor = load_watchlist_gsheets(st.session_state.sel_list)
    
    # 2. On vide le cache pour forcer la relecture du Sheets
    st.cache_data.clear()

# --- 5. INTERFACE ---
st.set_page_config(page_title="Analyseur Pro+", layout="wide")
st.markdown(
    """
    <style>
    /* On laisse un peu plus de place en haut du contenu principal pour le titre */
    .block-container {
        padding-top: 3.5rem !important;
    }
    
    /* Mais on garde la barre latérale bien haute */
    [data-testid="stSidebarNav"] {padding-top: 0rem;}
    [data-testid="stSidebarContent"] > div:first-child {padding-top: 1rem;}
    
    /* Police du tableau */
    [data-testid='stTable'] {font-size: 13px;}
    
    /* Resserre l'espace entre les éléments de la sidebar */
    .stVerticalBlock {gap: 0.5rem;}
    </style>
    """,
    unsafe_allow_html=True
)

with st.sidebar:
    # Ton bouton actuel à la ligne 178
    if st.button("🔄 Forcer l'actualisation", use_container_width=True):
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
                # 1. On récupère l'existant sur GSheets
                cur_tk = load_watchlist_gsheets(st.session_state.get('sel_list', 'Portefeuille Principal'))
                
                # 2. On prépare la nouvelle chaîne
                new_tickers_list = cur_tk + f", {tk_add}"
                
                # 3. SAUVEGARDE GOOGLE SHEETS
                save_watchlist_gsheets(st.session_state.get('sel_list', 'Portefeuille Principal'), new_tickers_list)
                
                # 4. MISE À JOUR DE LA MÉMOIRE DE LA ZONE DE TEXTE (La clé du problème)
                st.session_state["ticker_editor"] = new_tickers_list
                
                # 5. ON VIDE LE CACHE ET ON RELANCE
                st.cache_data.clear()
                st.rerun()

    st.divider()
    
    # --- ÉTAPE A : CRÉER (Pour ajouter un nouveau fichier) ou supprimer ---
    st.header("📂 Portefeuilles")
    lists = get_all_watchlists()
    sel_list = st.selectbox("Liste active :", lists, key='sel_list', on_change=on_list_change)

    # --- OPTIONS DE GESTION (Tiroirs) ---
    col1, col2 = st.columns(2)
    with col1:
        show_add = st.toggle("➕ Créer")
    with col2:
        show_del = st.toggle("🗑️", help="Supprimer un portefeuille")

    

    # Logique d'Ajout
    if show_add:
        st.info("Créer une nouvelle liste")
        new_name = st.text_input("Nom de la liste :", placeholder="Ex: Dividendes")
        if st.button("Confirmer Création", use_container_width=True):
            if new_name:
                save_watchlist_gsheets(new_name, "AAPL")
                st.success(f"'{new_name}' Liste créée !")
                st.cache_data.clear()
                import time
                time.sleep(0.5)
                st.rerun()
            else:
                st.error("Nom vide !")

    # Logic de Suppression (Version Google Sheets)
    if show_del:
        st.warning(f"⚠️ Action irréversible")
        list_to_del = st.selectbox("Choisir la liste à supprimer :", lists, key="del_select_box")
        
        if st.button(f"Confirmer la suppression de {list_to_del}", type="primary", key="btn_confirm_del"):
            if len(lists) > 1:
                try:
                    # 1. Connexion et lecture
                    conn = st.connection("gsheets", type=GSheetsConnection)
                    df_all = conn.read(worksheet="Watchlists")
                    
                    # 2. Suppression de la ligne (on garde tout SAUF list_to_del)
                    df_updated = df_all[df_all['list_name'] != list_to_del]
                    
                    # 3. Mise à jour sur Google Sheets
                    conn.update(worksheet="Watchlists", data=df_updated)
                    
                    # 4. Message et rafraîchissement
                    st.success(f"🔥 Liste '{list_to_del}' supprimée avec succès !")
                    st.cache_data.clear() # TRÈS IMPORTANT
                    
                    import time
                    time.sleep(0.5)
                    st.rerun()
                    
                except Exception as e:
                    st.error(f"Erreur lors de la suppression : {e}")
            else:
                st.error("🚫 Impossible de supprimer la dernière liste !")

# Ajout du bouton actualitées 
# 1. Ajouter de l'espace sous les boutons Créer/Supprimer
    st.sidebar.markdown("<br>", unsafe_allow_html=True) 
    # OU plus simplement : st.sidebar.write("") 

    # 2. Créer des colonnes très serrées pour rapprocher la case et le texte
    # Le ratio [0.5, 4] rapproche la col1 de la col2 au maximum
    col_news1, col_news2 = st.sidebar.columns([0.5, 4], vertical_alignment="center")

    with col_news1:
    # On laisse le label vide pour ne pas prendre de place
        show_news_portfolio = st.checkbox("", value=False, key="chk_news_port")

    with col_news2:
    # On colle l'icône et le texte ici
        st.markdown("📰 **Actualités**", help="Afficher les Actualités du portefeuille")
    
    st.divider()
    # --- ÉTAPE B : ÉDITER (Automatique via Ctrl+Entrée) ---
    current_content = load_watchlist_gsheets(sel_list)
    # 2. Si la mémoire est vide ou si on vient de changer, on force la synchro
    if "ticker_editor" not in st.session_state:
        st.session_state["ticker_editor"] = current_content

    tickers_input = st.text_area(
        "Éditer les tickers :", 
        value=current_content, 
        height=100, 
        key="ticker_editor",           # Identifiant pour la fonction
        on_change=update_tickers_callback # La fonction qui s'exécute au Ctrl+Entrée
    ).upper()
    st.divider()   
    cols_all = ["Nom", "Secteur", "Prix Actuel", "BNA Actuel", "PER Actuel", "BNA Forward", "PER Forward", 
                "Entrée BNA -15%", "Entrée FCF -15%", "Entrée Analystes -15%", "Entrée Synthèse (-15%)", 
                "Santé (Piotroski)", "Chg 1J", "Chg 1M", "Chg YTD", "Nb Analystes", "Dividende (€/$)", "Rendement %", "Date Détachement", "Avis Analystes"]

st.title(f"📈 {sel_list}")
# Cette ligne est "blindée" contre les espaces, les sauts de ligne et les minuscules
t_list = [t.strip().upper() for t in tickers_input.replace('\r', '').replace('\n', ',').split(',') if t.strip()]


if t_list:
    data_res = []
    for t in t_list:
        res = fetch_stock_data(t)
        if res:
            data_res.append(res)
        
        # 1. Message d'avertissement si aucune donnée n'est récupérée (ex: problème Yahoo ou tickers invalides)
        if not data_res:
            st.warning("⚠️ Trop de requettes envoyées ou les tickers sont invalides. Réessayez dans quelques minutes.")
            st.stop() # Cette ligne magique arrête le code ici SI data_res est vide
        # ------------------------
    
    df = pd.DataFrame(data_res)    
    # --- GESTION DES COLONNES VIA GOOGLE SHEETS ---
    try:
        # 1. Lecture de l'onglet de configuration
        df_conf = conn.read(worksheet="Choix_colonnes")
        
        # 2. Sélecteur de Profil dans la barre latérale
        liste_profils = sorted(df_conf['Profil'].unique().tolist())
        profil_choisi = st.sidebar.selectbox("📋 Vue de tableau", options=liste_profils)

        # 3. On filtre les réglages pour ce profil précis
        config_active = df_conf[df_conf['Profil'] == profil_choisi]
        
        # On récupère les colonnes "cochées" dans le Sheet
        cols_base = config_active[config_active['Afficher'] == True]['Nom_Colonne'].tolist()
        cols_figees_base = config_active[config_active['Figer'] == True]['Nom_Colonne'].tolist()

    except Exception as e:
        st.error(f"Erreur configuration colonnes : {e}")
        cols_base, cols_figees_base = ["Ticker", "Nom"], ["Ticker"]

    # --- 4. ENRICHISSEMENT ET MODIFICATION DYNAMIQUE ---
    with st.expander("🛠️ Personnaliser les colonnes affichées"):
        # On permet d'ajouter n'importe quelle colonne du DF principal
        toutes_les_cols = df.columns.tolist()
        
        selection_finale = st.multiselect(
            "Colonnes actives :",
            options=toutes_les_cols,
            default=[c for c in cols_base if c in toutes_les_cols]
        )
        
        # On permet de modifier quelles colonnes sont figées
        selection_figee = st.multiselect(
            "Colonnes à figer à gauche :",
            options=selection_finale,
            default=[c for c in cols_figees_base if c in selection_finale]
        )

    # --- 5. AFFICHAGE FINAL ---
    # On prépare la configuration "pinned" pour Streamlit
    config_colonnes = {col: st.column_config.Column(pinned=True) for col in selection_figee}

    
    def style_df(df):
        styles = pd.DataFrame('', index=df.index, columns=df.columns)
        
        # --- COLORATION DES CASES ---
        if 'Prix Actuel' in df.columns:
            p_actuel = df['Prix Actuel']
            
            # Entrées individuelles (Vert si > Prix)
            for col in [ 'Entrée FCF -15%', 'Entrée BNA -15%','Entrée Analystes -15%']:
                if col in df.columns:
                    mask = df[col].fillna(0) > p_actuel
                    styles.loc[mask, col] = 'background-color: #d4edda; color: #155724;'

            # Entrée Synthèse (Vert si > Prix + Mise en avant)
            if 'Entrée Synthèse (-15%)' in df.columns:
                mask_synth = df['Entrée Synthèse (-15%)'] > p_actuel
                # Style de base pour la colonne (Bordure et gras)
                styles['Entrée Synthèse (-15%)'] = 'border-left: 2px solid #555; border-right: 2px solid #555; font-weight: bold;'
                # Coloration si signal achat
                styles.loc[mask_synth, 'Entrée Synthèse (-15%)'] += 'background-color: #28a745; color: white;'

        # --- COLORATION PIOTROSKI ---
        if 'Santé (Piotroski)' in df.columns:
            for i, v in df['Santé (Piotroski)'].items():
                try:
                    s = int(str(v).split('/')[0])
                    if s >= 4: styles.loc[i, 'Santé (Piotroski)'] += 'color: #28a745; font-weight: bold;'
                    elif s <= 1: styles.loc[i, 'Santé (Piotroski)'] += 'color: #dc3545; font-weight: bold;'
                except: pass
        # --- COLORATION DES PERFORMANCES ---
        for col in ['Chg 1J', 'Chg 1M', 'Chg YTD']:
            if col in df.columns:
                # On cherche le signe + ou - dans le texte (car ce sont des strings avec emojis)
                mask_plus = df[col].astype(str).str.contains('\+')
                mask_moins = df[col].astype(str).str.contains('-')
                
                # On applique les couleurs (Vert pour +, Rouge pour -)
                styles.loc[mask_plus, col] += 'color: #28a745; font-weight: bold;'
                styles.loc[mask_moins, col] += 'color: #dc3545; font-weight: bold;'
        return styles
    if show_news_portfolio:
        # 1. MODE REVUE DE PRESSE (S'affiche à la place du tableau)
        st.subheader(f"🗞️ Revue de Presse : {sel_list}")
                    
        if tickers_input:
            # 1. On prépare la liste à partir de l'input
            liste_tickers = [t.strip().upper() for t in tickers_input.split(',') if t.strip()]
            
            # 2. On appelle la fonction "Fragment" qu'on a créée à l'étape 1
            # --- SÉLECTEUR DE VUE ---
            mode_vue = st.radio("", ["⏳ Flux Chronologique" , "🏢 Par Action"], horizontal=True)
            
            if mode_vue == "⏳ Flux Chronologique":
                news_timeline_module(liste_tickers)
            else:
                news_dashboard_module(liste_tickers)
                
        else:
            st.info("La liste de tickers est vide.")
    else:
        sel = st.dataframe(
            df[selection_finale].style.apply(style_df, axis=None).format(formatter=lambda x: clean_num(x) if isinstance(x, (int, float)) else x),
            on_select="rerun",
            selection_mode="single-row",
            use_container_width=True,
            hide_index=True,
            height=1000,
            column_config=config_colonnes 
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
                # --- SECTION GRAPHIQUE ---
                # --- SECTION GRAPHIQUE AVANCÉ (PRIX + VOLUME) ---
                st.divider()
                st.subheader(f"📈 Performance & Volumes (YTD)")
                
                try:
                    
                    
                    s_obj = yf.Ticker(d['Ticker'])
                    current_yr = datetime.now().year
                    
                    # Récupération historique pour calcul MA50
                    from datetime import timedelta
                    date_debut_calcul = (datetime(current_yr, 1, 1) - timedelta(days=100)).strftime('%Y-%m-%d')
                    h_data_large = s_obj.history(start=date_debut_calcul)

                    if not h_data_large.empty:
                        h_data_large['MA50'] = h_data_large['Close'].rolling(window=50).mean()
                        h_data = h_data_large[h_data_large.index >= f"{current_yr}-01-01"]

                        # --- 1. CALCUL COULEUR VOLUME (Vert si hausse, Rouge si baisse) ---
                        colors = ['#28a745' if row['Close'] >= row['Open'] else '#dc3545' 
                                for _, row in h_data.iterrows()]

                        fig = make_subplots(specs=[[{"secondary_y": True}]])

                        # Courbe du prix
                        fig.add_trace(go.Scatter(x=h_data.index, y=h_data['Close'], name="Prix", line=dict(color='#28a745', width=2)), secondary_y=False)
                        
                        # MA50
                        fig.add_trace(go.Scatter(x=h_data.index, y=h_data['MA50'], name="MA50", line=dict(color='orange', dash='dot')), secondary_y=False)

                        # Volumes colorés
                        fig.add_trace(go.Bar(x=h_data.index, y=h_data['Volume'], name="Volume", marker_color=colors, opacity=0.3), secondary_y=True)

                        # --- 2. TRACÉ DES LIGNES HORIZONTALES ---
                        prix_actuel = d['Prix Actuel']
                        # Ligne Prix Actuel
                        fig.add_hline(y=prix_actuel, line_dash="dash", line_color="gray", 
                                    annotation_text=f"Actuel: {prix_actuel}", annotation_position="bottom right")
                        
                        # Ligne Zone d'Achat (-15%)
                        prix_achat = prix_actuel * 0.85
                        fig.add_hline(y=prix_achat, line_dash="dot", line_color="#28a745", 
                                    annotation_text="Zone d'achat (-15%)", annotation_position="top left")

                        # Mise en forme
                        # On récupère le nom et le ticker pour le titre
                        nom_action = d.get('Nom', 'Action')
                        ticker_action = d.get('Ticker', '')
                        fig.update_layout(
                            title={
                                'text': f" {nom_action} ({ticker_action})",
                                'y': 0.95,
                                'x': 0.5,
                                'xanchor': 'center',
                                'yanchor': 'top',
                                'font': {'size': 20}
                            },
                            height=450, margin=dict(l=0, r=0, t=30, b=0), hovermode="x unified", template="plotly_white")
                        fig.update_yaxes(title_text="Prix", secondary_y=False, showgrid=True, gridcolor='lightgray', fixedrange=False)
                        fig.update_yaxes(title_text="Volume", secondary_y=True, showgrid=False, fixedrange=False)

                        st.plotly_chart(fig, use_container_width=True,
                                        config={
                                            'scrollZoom': True,        # Active la roulette
                                            'displayModeBar': True, 
                                            'editable': True,  # Affiche la barre d'outils en haut à droite
                                            'modeBarButtonsToAdd': [
                                                'drawline',     # Tracer des lignes droites
                                                'drawrect',     # Tracer des zones (rectangles)
                                                'eraseshape'    # Gomme pour effacer tes tracés
                                            ],
                                            'displaylogo': False       # Enlève le logo Plotly
                                            }
                        )
                    else:
                        st.info("Données non disponibles.")
                except Exception as e:
                    st.error("Installez plotly pour voir ce graphique : pip install plotly")

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

                # --- BLOC NEWS SÉCURISÉ ---
                st.divider()
                st.subheader("📰 Dernières Actualités")
                ticker_brut = d.get('Ticker', 'AAPL')
                ticker_clean = ticker_brut.split('.')[0].upper()
                all_news = []
                
                # On récupère le ticker et le nom depuis le dictionnaire 'd'
                t_brut = d.get('Ticker') or d.get('ticker') or "AAPL"
                n_brut = d.get('Nom') or d.get('nom') or t_brut

                # On prépare les versions propres pour le filtrage
                t_clean = str(t_brut).split('.')[0].upper()
                n_clean = str(n_brut).replace(" S.A.", "").replace(" SA", "").replace(" Inc", "").replace(", Inc.", "")
                
                # --- DÉFINITION de  LA BLACKLIST GLOBALE ---
                # Ces mots indiquent souvent des pubs, des listes d'actions ou du contenu robotisé
                blacklist = [
                    "SPONSORED", "PROMO", "DEAL OF THE DAY", "TOP 10", "WEEKLY ROUNDUP", 
                    "LISTE D'ACTIONS", "SÉLECTION", "PANIER", "MEILLEURES ACTIONS",
                    "TRADING BOT", "YIELD", "CRYPTO", "FOREX"
                ]

                # --- 1. RÉCUPÉRATION GOOGLE NEWS (FR) ---
                try:
                    # On utilise le nom simplifié pour la recherche
                    query_name = n_clean.split(' ')[0].strip()
                    url_fr = f"https://news.google.com/rss/search?q={query_name}+bourse+when:7d&hl=fr&gl=FR&ceid=FR:fr"
                    feed = feedparser.parse(url_fr)

                    for entry in feed.entries:
                        title_upper = entry.title.upper()
                    
                    # 1. Vérification de la pertinence (Nom ou Ticker)
                    is_relevant = (query_name.upper() in title_upper) or (t_clean in title_upper)
                    
                    # 2. Filtrage par Blacklist (Pubs, Listes, etc.)
                    if is_relevant and any(word in title_upper for word in blacklist):
                        is_relevant = False
                    
                    # 3. FILTRE ANTI-BRUIT (Nouveau) : Éviter les articles qui citent trop d'actions
                    # Si un titre contient trop de virgules ou de symboles '$', c'est souvent une liste de cours
                    if is_relevant and (title_upper.count(',') > 3 or title_upper.count('$') > 2):
                        is_relevant = False

                    # 4. Ajout final si toujours valide
                    if is_relevant:
                        dt_obj = datetime(*entry.published_parsed[:6])
                        all_news.append({
                            'timestamp': dt_obj,
                            'date_visuelle': dt_obj.strftime('%d/%m'),
                            'titre': entry.title,
                            'source': f"🇫🇷 {entry.source.get('title', 'Google')}",
                            'lien': entry.link,
                        })
                except Exception as e:
                    print(f"Erreur Google News pour {t_clean}: {e}")

                # --- 2. SOURCE ALTERNATIVE : FINVIZ (US) ---
                try:
                    url_finviz = f"https://finviz.com/quote.ashx?t={t_clean}"
                    headers = {'User-Agent': 'Mozilla/5.0'}
                    response = requests.get(url_finviz, headers=headers, timeout=10)

                    if response.status_code == 200:
                        soup = BeautifulSoup(response.content, 'html.parser')
                        news_table = soup.find(id='news-table')
                        if news_table:
                            for row in news_table.findAll('tr')[:10]:
                                a_tag = row.find('a')
                                if a_tag:
                                    text = a_tag.get_text()
                                    if t_clean in text.upper():
                                        # Vérification blacklist pour Finviz aussi
                                       if not any(word in text.upper() for word in blacklist):
                                            all_news.append({
                                                'timestamp': datetime.now(),
                                                'date_visuelle': datetime.now().strftime('%d/%m'),
                                                'titre': text,
                                                'source': "🇺🇸 Finviz",
                                                'lien': a_tag['href'],
                                            })
                except Exception as e:
                    print(f"Erreur Finviz pour {t_clean}: {e}")
                
                            

                # --- 3. Affichage ---
                # --- 3. TRI ET AFFICHAGE AVEC ANALYSE DE SENTIMENT ---
                if all_news:
                    all_news.sort(key=lambda x: x['timestamp'], reverse=True)
                    
                    for article in all_news[:12]:
                        # Analyse de sentiment avec TextBlob
                        analysis = TextBlob(article['titre'])
                        polarity = analysis.sentiment.polarity  # Score entre -1 et 1
                        
                        # Définition de l'emoji et de la couleur selon le score
                        if polarity > 0.1:
                            sentiment_icon = "🟢"  # Positif
                            sentiment_label = "Bullish"
                        elif polarity < -0.1:
                            sentiment_icon = "🔴"  # Négatif
                            sentiment_label = "Bearish"
                        else:
                            sentiment_icon = "⚪"  # Neutre
                            sentiment_label = "Neutre"

                        # Affichage du label avec le sentiment
                        label = f"{sentiment_icon} **{article['date_visuelle']}** | {article['titre']}"
                        
                        with st.expander(label):
                            st.write(f"**Source :** {article['source']}")
                            st.write(f"**Sentiment :** {sentiment_label} (Score: {round(polarity, 2)})")
                            st.link_button("Lire l'article", article['lien'])
                else:
                    st.info(f"ℹ️ Aucune actualité récente disponible pour {ticker_clean}: {e}.")


