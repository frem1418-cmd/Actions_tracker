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

# Initialisation de la connexion (à faire une seule fois)
conn = st.connection("gsheets", type=GSheetsConnection)

@st.cache_data(ttl=900) # On garde en mémoire 15 min
def get_bundle_news(liste_tickers, ticker_to_name=None):
    if ticker_to_name is None:
        ticker_to_name = {} # Dico vide par défaut
    all_news_combined = []
    
    # On lance la récupération pour TOUS les tickers en même temps (max 15 threads)
    with ThreadPoolExecutor(max_workers=15) as executor:
        # On crée un dictionnaire pour suivre quel thread correspond à quel ticker
        future_to_ticker = {executor.submit(get_quick_news, t): t for t in liste_tickers}
        
        for future in future_to_ticker:
            ticker_parent = future_to_ticker[future]
            try:
                articles = future.result(timeout=10) # 10s max par ticker
                if articles:
                    for a in articles:
                        # On injecte le ticker d'origine dans chaque news pour savoir d'où elle vient
                        a['ticker_parent'] = ticker_parent
                        a['nom_propre'] = ticker_to_name.get(ticker_parent, ticker_parent)
                        all_news_combined.append(a)
            except Exception as e:
                print(f"Erreur sur {ticker_parent}: {e}")
                continue
                
    return all_news_combined

# Fonction de traduction sécurisée avec cache pour éviter les appels redondants
@st.cache_data(ttl=3600)
def safe_translate(text):
    if not text or len(text) < 5:
        return text
    try:
        return GoogleTranslator(source='auto', target='fr').translate(text)
    except:
        return text

@st.cache_data(ttl=3600)
# Fonction de traduction en batch pour les titres (plus rapide que de traduire un par un)
def translate_batch(titles_list):
    if not titles_list:
        return []
    try:
        # On fusionne les titres avec un séparateur que le traducteur respecte
        combined_text = " ||| ".join(titles_list)
        translated_text = GoogleTranslator(source='auto', target='fr').translate(combined_text)
        
        # On redécoupe pour récupérer chaque titre individuel
        translated_list = [t.strip() for t in translated_text.split("|||")]
        
        # Sécurité : si le nombre de titres ne correspond pas, on renvoie l'original
        if len(translated_list) != len(titles_list):
            return titles_list
            
        return translated_list
    except Exception as e:
        print(f"Erreur batch translation: {e}")
        return titles_list
   
#Fonction pour récupérer les news et analyser le sentiment
@st.cache_data(ttl=900) # Les news sont gardées en mémoire 15 min
def get_quick_news(ticker):
    news_list = []
    t_clean = ticker.split('.')[0].strip().upper()
    # On récupère la date du jour au format Google pour comparer    
    today_str = datetime.now().strftime("%d %b")
    # --- 1. Google News FR ---

    # On utilise une fonction de parsing unique pour tous les flux Google (FR/US) en passant le badge en paramètre
    def process_general_google(url, badge_icon, default_source="Info", limit=10):
        news_list = []
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        try:
            r = requests.get(url, headers=headers, timeout=5)
            if r.status_code == 200:
                feed = feedparser.parse(r.text)
                # Utilisation de la variable 'limit
                for e in feed.entries[:limit]:
                    # --- NETTOYAGE TITRE ET SOURCE  ---
                    parts = e.title.rsplit(' - ', 1)
                    clean_title = parts[0]
                    source_name = parts[1] if len(parts) > 1 else default_source
                    
                    # --- ANALYSE SENTIMENT  ---
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
                        'badge': f"{icon_sent} {badge_icon}", # Ex: 🟢 💎
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
        # Requête groupée pour Bloomberg et Reuters
        url = f"https://news.google.com/rss/search?q={t_clean}+source:Bloomberg+OR+source:Reuters&hl=en-US"
        return process_general_google(
            url, 
            badge_icon="💎"
            )


    def fetch_google_wires(t_clean):
        # Requête pour les communiqués officiels
        url = f"https://news.google.com/rss/search?q={t_clean}+source:PR_Newswire+OR+source:Business_Wire&hl=en-US"
        return process_general_google(
            url,
            badge_icon="📄",
            limit=20
            )
    
    def fetch_benzinga_fixed(t_clean):
        # Correction de l'URL Benzinga suite à ton erreur 404 (Image 92a4b3)
        #url = f"https://www.benzinga.com/stock/{t_clean}/rss"
        url = "https://www.benzinga.com/markets/feed"
        return process_general_google(url, "⚡ Benzinga", default_source="Benzinga")

    # --- Seeking Alpha US ---
    def fetch_seeking(t_clean):
        #Récupère les analyses de Seeking Alpha"""
        news_list = []
        # URL spécifique au ticker
        url = f"https://seekingalpha.com/symbol/{t_clean}/feed"
         # On appelle le moteur avec le badge spécifique 🧡
        return process_general_google(
            url, 
            badge_icon="[:orange[a]]", 
            default_source="Seeking Alpha",
            limit=3
            )
    
    # EXÉCUTION EN PARALLÈLE
    @st.cache_data(ttl=900)
    def get_quick_news(ticker):
        news_list = []

        t_clean = ticker.split('.')[0].strip().upper() #t_clean = ticker.split('.')[0].strip().upper()

    # 1. Liste des fonctions à appeler
    tasks = []
    if '.PA' in ticker.upper():
        tasks.append(fetch_google_fr) #
    else:
        # On ajoute toutes les sources US en parallèle
        tasks.extend([
            fetch_google_us,
            fetch_google_agencies,
            fetch_google_wires,
            fetch_benzinga_fixed, #
            fetch_seeking         
        ])

    # 2. Exécution simultanée (Vitesse maximale)
    with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
        # Ici, toutes les fonctions reçoivent t_clean
        futures = [executor.submit(task, t_clean) for task in tasks]
        for future in futures:
            try:
                news_list.extend(future.result(timeout=10)) # Timeout augmenté pour SA
            except:
                continue

    # --- LE TRI FINAL (Plus récent en haut) ---
    # On trie la liste par l'objet 'dt_obj' du plus récent au plus ancien
    news_list.sort(key=lambda x: x['dt_obj'], reverse=True)
    # --- NOUVEL AFFINAGE DES DATES POUR L'AFFICHAGE ---
    now = datetime.now()
    for item in news_list:
        dt = item['dt_obj']
        # Si c'est aujourd'hui, on met "Auj. HH:MM"
        if dt.date() == now.date():
            item['date'] = dt.strftime('Auj. %H:%M')
        # Si c'est hier, on met "Hier HH:MM"
        elif (now.date() - dt.date()).days == 1:
            item['date'] = dt.strftime('Hier %H:%M')
        # Sinon format standard
        else:
            item['date'] = dt.strftime('%d/%m %H:%M')
    return news_list
# ---  FONCTIONS de rafraichissement de news ---
@st.cache_data(ttl=86400) # Cache le nom 24h
def get_action_name(ticker):
    try:
        return yf.Ticker(ticker).info.get('longName', ticker)
    except:
        return ticker
    
@st.fragment(run_every="5m") # Les news s'actualisent toutes seules toutes les 5 minutes
def news_dashboard_module(liste_tickers):
    # Barre d'outils discrète en haut du module
    
    col1, col2 = st.columns([0.8, 0.2])
    with col1:
        st.subheader("🗞️ Flux d'actualités en direct")
    with col2:
        if st.button("🔄 Actualiser", key="ref_action"):
            get_quick_news.clear()  # Force la recherche de nouvelles news
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


# --- Boucle actualite --- Fonction actualite  Flux ou Timeline- liste chronologique des news. Toutes la conf est dans le module ---
@st.fragment(run_every="5m")
def actualite_module(liste_tickers):
    # --- 1. BARRE D'OUTILS  ---
    col_search, col_sent, col_trad, col_ref = st.columns([0.4, 0.2, 0.2, 0.2])
    with col_search:
        # Champ de recherche
        query = st.text_input(
            "🔍 Rechercher...",
            placeholder="Action, mot-clé...",
            label_visibility="collapsed",
            key="news_search_input").lower().strip()
    
    with col_trad:
        # Toggle pour la traduction globale
        mode_global_fr = st.toggle("🇫🇷", help="Traduction des titres en français", 
                                   value=st.session_state.get('mode_fr', True), 
                                   key="mode_fr")
    
    with col_ref:
        # Bouton de rafraîchissement local au fragment
        if st.button("🔄", help="Actualiser le flux", key="refresh_news_btn"):
            get_quick_news.clear()  # Efface le cache des news pour forcer la récupération de nouvelles données
            st.rerun(scope="fragment")
    if 'nb_news_display' not in st.session_state:
        st.session_state.nb_news_display = 40 # Nombre de news à afficher par défaut

    with col_sent:
        filtre_sent = st.selectbox(
            "Filtrer par sentiment",
            options=["Tous", "Positifs 🟢", "Négatifs 🔴"],
            label_visibility="collapsed",            
        )

    # --- 2. COLLECTE GROUPÉE (LA MAGIE OPÈRE ICI) ---
    with st.spinner("Récupération des actualités..."):
        # Un seul appel pour tous les tickers !
        all_news = get_bundle_news(liste_tickers, ticker_to_name)

    # Tri chronologique global (une seule fois pour tout le monde)
    all_news.sort(key=lambda x: x.get('dt_obj', datetime.now()), reverse=True)

    # --- 3. DÉDUPLICATION ET FILTRAGE ---
    unique_news = []
    titres_vus = set()
    
    for n in all_news:        
        fingerprint = n['titre'].lower().strip()
        # 1. On récupère le sentiment et on prépare le filtre
        sent_label = n.get('sentiment', 'Neutre')
        match_sent = True
        
        # 2. On vérifie si la news doit être exclue selon le choix de l'utilisateur
        if "Positifs" in filtre_sent and sent_label != "Positif":
            match_sent = False
        elif "Négatifs" in filtre_sent and sent_label != "Négatif":
            match_sent = False

        # 3. ON N'ENTRE ICI QUE SI LE SENTIMENT EST OK
        if match_sent: 
            if fingerprint not in titres_vus:
                # Préparation des variables pour la recherche
                source_brut = n.get('source', '').lower()
                nom_brut = n.get('nom_propre', '').lower()
                
                # 4. Filtre de recherche par mot-clé (query)
                if not query or (query in fingerprint or 
                                query in n.get('ticker_parent', '').lower() or 
                                query in source_brut or 
                                query in nom_brut):
                    
                    # 5. AJOUT FINAL (Indentation maximum ici)
                    unique_news.append(n)
                    titres_vus.add(fingerprint)

    # --- 4. AFFICHAGE FINAL ---
    st.markdown("---")
    if unique_news:
        # --- OPTIMISATION : Traduction groupée avant la boucle ---
        news_to_display = unique_news[:st.session_state.nb_news_display]

        if st.session_state.get('mode_fr', False):
            with st.spinner("Traduction des titres..."):
                titres_originaux = [n['titre'] for n in news_to_display]
                titres_traduits = translate_batch(titres_originaux)
                
                # On injecte les traductions dans nos objets news
                for i, n in enumerate(news_to_display):
                    if i < len(titres_traduits):
                        n['titre_affiche'] = titres_traduits[i]
        else:
            # Mode anglais : le titre affiché est le titre original
            for n in news_to_display:
                n['titre_affiche'] = n['titre']

        # --- BOUCLE D'AFFICHAGE (Maintenant ultra rapide) ---
        for n in news_to_display:
            titre_final = n.get('titre_affiche', n['titre'])
            
            source = n.get('source', 'Info')
            nom_action = n.get('nom_propre', n.get('ticker_parent', 'Action'))
            
            # Ligne finale élégante
            st.markdown(
                f"{n['badge']} | {n['date']} | **{nom_action}** : "
                f"[{titre_final}]({n['lien']}) *({source})*"
        )
        
    else:
        st.info("Aucune actualité trouvée.")
    # --- 5. BOUTON AFFICHER PLUS (SI PLUS DE NEWS DISPONIBLES) ---
    if len(unique_news) > st.session_state.nb_news_display:
            st.write("---") # Une petite ligne de séparation
            
            # On crée 3 colonnes pour centrer le bouton
            c1, c2, c3 = st.columns([1, 2, 1])
            with c2:
                if st.button(f"Afficher plus de news (+40) ➕", use_container_width=True):
                    st.session_state.nb_news_display += 40
                    st.rerun()    

# --- FONCTION DE CHARGEMENT DES WATCHLISTS DEPUIS GOOGLE SHEETS ---
@st.cache_data(ttl=3600) # On garde la liste en mémoire 1 heure pour éviter les appels redondants à Google Sheets
def load_all_watchlists():
    try:
        conn = st.connection("gsheets", type=GSheetsConnection)
        df = conn.read(worksheet="Watchlists")
        return df
    except Exception:
        return None
    
@st.cache_data(ttl=600) # Cache de 10 minutes pour la sélection
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
    
    # On lance la récupération pour tous les tickers en même temps
    with ThreadPoolExecutor(max_workers=10) as executor:
        # On utilise une compréhension de liste pour lancer get_quick_news
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
    # On utilise la connexion définie globalement
    return conn.read(worksheet="Choix_colonnes")
    
# --- FONCTION DE SAUVEGARDE DES WATCHLISTS VERS GOOGLE SHEETS ---    
def update_tickers_callback():
    # On récupère le texte saisi dans le text_area via sa clé
    new_val = st.session_state["ticker_editor"].upper()
    # On sauvegarde dans GSheets
    save_watchlist_gsheets(sel_list, new_val)
    # On vide le cache pour que le tableau se mette à jour avec les nouveaux cours
    st.cache_data.clear()
# --- 1. CONFIGURATION & DOSSIERS ---

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
        response = requests.get(url, headers=headers, timeout=5)
        data = response.json()
        results = []
        for res in data.get('quotes', []):
            if res.get('quoteType') == 'EQUITY':
                label = f"{res.get('symbol')} - {res.get('longname')} ({res.get('exchDisp')})"
                results.append({"label": label, "symbol": res.get('symbol')})
        return results
    except: return []

# Fonction de formatage des nombres pour les rendre plus lisibles (ex: 1500000 -> 1.5 M)
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
@st.cache_data(ttl=3600)
def get_all_watchlists():
    try:
        conn = st.connection("gsheets", type=GSheetsConnection)
        df = conn.read(worksheet="Watchlists")
        
        watchlists_dict = {}
        if not df.empty and 'list_name' in df.columns:
            # On groupe les tickers par nom de liste
            # On suppose que tes tickers sont dans une colonne 'tickers' ou 'Ticker'
            col_ticker = 'tickers' if 'tickers' in df.columns else 'Ticker'
            
            for name in df['list_name'].dropna().unique():
                # On récupère la chaîne de tickers (ex: "MSFT, AAPL")
                t_data = df[df['list_name'] == name][col_ticker].iloc[0]
                # On transforme la chaîne en liste propre
                ticker_list = [t.strip().upper() for t in str(t_data).split(',') if t.strip()]
                watchlists_dict[name] = ticker_list
            
            return watchlists_dict
        return {"Actions_EU": ["AAPL"]} # Secours
    except:
        return {"Actions_EU": ["AAPL"]} # Secours


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
    sel_list = st.selectbox(
        "Liste active :", 
        options=list(lists.keys()), 
        key='sel_list', 
        on_change=on_list_change
    )
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


@st.cache_data(ttl=3600)
def get_column_config():
    # Cette fonction ne sera exécutée réellement qu'une fois par heure
    return conn.read(worksheet="Choix_colonnes")
# Bloc principal : boucle qui recupere les infos de tous les tickers et affiche le tableau
if t_list:
    status_container = st.empty()
    # Création d'une barre de progression
    with status_container.container():
        with st.status(f"⏳ Analyse de {len(t_list)} actions en cours...", expanded=True) as status:
            st.write("Connexion aux serveurs financiers...")
            
            # Exécution en parallèle
            with ThreadPoolExecutor(max_workers=20) as executor:
                results = list(executor.map(fetch_stock_data, t_list))
            
            st.write("Finalisation des calculs...")
            
            # On change l'apparence une fois fini
            status.update(label="✅ Données prêtes !", state="complete", expanded=False)
            # Petit délai optionnel de 0.5s pour que l'utilisateur 
            # voit le message "Terminé" avant que ça disparaisse            
            time.sleep(0.5)

    # ON EFFACE TOUT le conteneur de statut
    status_container.empty()

    # On traite les résultats et on affiche le tableau (qui prendra la place vide)
    data_res = [r for r in results if r is not None]
    
    if data_res:
        # Affiche ici le tableau final (st.dataframe ou st.table)
        df = pd.DataFrame(data_res)

        #Convertir la colonne 'Date Détachement' en format date (ajuste le nom exact de la colonne)
        # dayfirst=True est important si tes dates sont au format JJ/MM/AAAA
        df['Date Détachement'] = pd.to_datetime(df['Date Détachement'], errors='coerce', dayfirst=True)    
        ticker_to_name = dict(zip(df['Ticker'], df['Nom']))
        # --- GESTION DES COLONNES VIA GOOGLE SHEETS ---
        try:
            # 1. Lecture de l'onglet de configuration
            df_conf = get_column_config()
            
            # 2. Sélecteur de Profil dans la barre latérale
            liste_profils = sorted(df_conf['Profil'].unique().tolist())
            profil_choisi = st.sidebar.selectbox("📋 Vue de tableau", options=liste_profils)

            # 3. On filtre les réglages pour ce profil précis
            config_active = df_conf[df_conf['Profil'] == profil_choisi]            
            cols_base = config_active[config_active['Afficher'] == True]['Nom_Colonne'].tolist()
            cols_figees_base = config_active[config_active['Figer'] == True]['Nom_Colonne'].tolist()

        except Exception as e:
            st.error(f"Erreur configuration colonnes : {e}")
            cols_base, cols_figees_base = ["Ticker", "Nom"], ["Ticker"]

    
        # --- 5. AFFICHAGE FINAL ---
        # On prépare la configuration "pinned" pour Streamlit
        selection_finale = []
        selection_figee = []
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
        # 1. Titre de la section
        st.subheader(f"📝 Revue de Presse : {sel_list}")

        if tickers_input:
            # On prépare la liste à partir de l'input
            liste_tickers = [t.strip().upper() for t in tickers_input.split(',') if t.strip()]
            
            # ON APPELLE DIRECTEMENT LE FLUX CHRONOLOGIQUE (Plus besoin de radio bouton)
            actualite_module(liste_tickers)
        else:
            st.info("La liste de tickers est vide.")
    else:
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

        # Calcul dynamique : 35 pixels par ligne + 38 pixels pour l'en-tête
        hauteur_dynamique = (len(df) * 35) + 38
        sel = st.dataframe(
            df[selection_finale].style.apply(style_df, axis=None).format(formatter=lambda x: clean_num(x) if isinstance(x, (int, float)) else x),
            on_select="rerun",
            selection_mode="single-row",
            use_container_width=True,
            hide_index=True,
            height=min(hauteur_dynamique, 850),
            column_config={
                "Date Détachement": st.column_config.DateColumn(
                    "Date Détachement",
                    format="DD/MM/YYYY",  # Force l'affichage au format français
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

                # --- Mise en place et affichage de la partie "Dernières Actualités dans la vue avancée de l'action"  ---
                mode_fr = False  # Valeur par défaut pour éviter l'erreur Pylance
                ticker_clean = "AAPL"
                # --- 1. IDENTIFICATION DU TICKER ---
                if d and 'Ticker' in d:
                    # On nettoie le ticker (ex: MC.PA -> MC)
                    ticker_clean = str(d['Ticker']).split('.')[0].upper()
                    nom_action_vue = d.get('Nom', ticker_clean) # On récupère le nom depuis le DataFrame
                else:
                    ticker_clean = "AAPL"
                    nom_action_vue = "Apple"

                #  --- Affichage bouton DANS LA VUE DÉTAILLÉE ---
                st.divider()
                # Création d'une ligne avec Titre à gauche et Bouton à droite
                col_titre, col_switch = st.columns([3, 1])
                
                with col_titre:
                    st.markdown(f"### 📰 Dernières Actualités : {nom_action_vue}")
                with col_switch:
                    # On affiche le bouton ici aussi. 
                    # TRÈS IMPORTANT : Utilise la même clé 'mode_fr' pour qu'ils soient synchronisés !
                    mode_fr = st.toggle("FR", help="Traduction automatique des titres en français", value=mode_fr)

                # --- 2. COLLECTE ET TRI ---
                all_news = get_quick_news(ticker_clean)

                if all_news:
                    # Tri chronologique robuste
                    all_news.sort(key=lambda x: x.get('dt_obj', datetime.now()), reverse=True)
                    
                    unique_news = []
                    titres_vus = set()
                    
                    for article in all_news:
                        t_brut = article.get('titre', '').lower().strip()
                        if t_brut not in titres_vus:
                            unique_news.append(article)
                            titres_vus.add(t_brut)
                
                query = st.session_state.get("main_search", "") # Si tu as une barre de recherche

                if query:
                    q = query.lower()
                    unique_news = [
                        a for a in unique_news 
                        if q in a.get('titre', '').lower() 
                        or q in a.get('source', '').lower()
                        or q in a.get('ticker_parent', '').lower()
                    ]

                # --- 3. BOUCLE D'AFFICHAGE ---
                for article in unique_news[:20]:
                    
                    lien_reel = article.get('lien', '#') 
                    source = article.get('source', 'Info').strip('() ')
                    date = article.get('date', 'Auj.')
                    badge = article.get('badge', '🌐')
                    titre_brut = article.get('titre', 'Sans titre')
                    is_seeking = "seekingalpha.com" in lien_reel.lower()
                    # Détection anglais
                    mots_en = {'the', 'stock', 'growth', 'fed', 'market', 'earnings'}
                    est_anglais = any(w in titre_brut.lower() for w in mots_en) or "seekingalpha" in lien_reel.lower()
                    
                    # Traduction
                    if mode_fr and est_anglais:
                        titre_affiche = safe_translate(titre_brut)
                    else:
                        titre_affiche = titre_brut

                    label = f"{badge} | **{date}** | {titre_affiche}"

                    with st.expander(label):
                        st.write(f"**Origine :** {source}")
                        
                        if is_seeking or not est_anglais:
                            # Article déjà en français
                            st.link_button("📖 Lire l'article complet", lien_reel, use_container_width=True)
                        else:
                            # Article Anglais : Double bouton comme tu aimais
                            c1, c2 = st.columns(2)
                            with c1:
                                st.link_button("📄 Original (EN)", lien_reel, use_container_width=True)
                            with c2:
                                # On affiche le bouton Google Translate pour toutes les autres sources
                                lien_propre = urllib.parse.quote(lien_reel, safe='')
                                url_t = f"https://translate.google.com/translate?sl=auto&tl=fr&u={lien_propre}"
                                #url_t = f"https://translate.google.com/translate?sl=auto&tl=fr&u={lien_reel}"
                                st.link_button("🇫🇷 Traduire Page", url_t, type="primary", use_container_width=True)
                        
                        if mode_fr and est_anglais:
                            st.caption(f"Original : {titre_brut}")