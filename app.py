import streamlit as st
import yfinance as yf
import pandas as pd
import numpy as np
import requests
import re
import io
import os
import feedparser
from datetime import datetime, timedelta
from textblob import TextBlob
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import time
from bs4 import BeautifulSoup
import streamlit as st
from streamlit_gsheets import GSheetsConnection
from deep_translator import GoogleTranslator, MyMemoryTranslator
from concurrent.futures import ThreadPoolExecutor as _BaseThreadPoolExecutor
import threading
import urllib.parse
import logging
from streamlit.runtime.scriptrunner import add_script_run_ctx, get_script_run_ctx
import streamlit.components.v1 as components
try:
    from pypdf import PdfReader  # lecture du PDF de rapport de résultats uploadé par l'utilisateur
    _PYPDF_DISPONIBLE = True
except ImportError:
    _PYPDF_DISPONIBLE = False
logging.getLogger("streamlit").setLevel(logging.ERROR)
# Les 401/"Invalid Crumb" qu'on voit dans les logs viennent de l'endpoint Yahoo
# (bloqué/rate-limité par intermittence sur les IP "cloud" comme Streamlit Cloud) :
# le code a déjà un repli automatique (historique yfinance, puis appel individuel)
# quand ça échoue, donc ce n'est pas un bug fonctionnel — juste yfinance qui logge
# chaque échec en ERROR. On baisse son niveau de log pour ne garder que l'essentiel.
logging.getLogger("yfinance").setLevel(logging.CRITICAL)


class ThreadPoolExecutor(_BaseThreadPoolExecutor):
    """Remplace concurrent.futures.ThreadPoolExecutor partout dans ce fichier : propage
    le ScriptRunContext de Streamlit à chaque thread du pool. Sans ça, Streamlit logue
    un warning 'missing ScriptRunContext!' pour chaque thread créé pendant l'exécution
    du script (visible dans les logs Streamlit Cloud) — inoffensif mais bruyant.
    `Executor.map()` appelle `submit()` en interne, donc override suffit pour couvrir
    tous les usages (map ET submit) sans toucher aux appels existants."""

    def submit(self, fn, *args, **kwargs):
        ctx = get_script_run_ctx()

        def _wrapped(*a, **kw):
            if ctx is not None:
                add_script_run_ctx(threading.current_thread(), ctx)
            return fn(*a, **kw)

        return super().submit(_wrapped, *args, **kwargs)

# Session HTTP interne de yfinance : elle gère automatiquement le "crumb"/cookie
# désormais exigé par Yahoo Finance sur ses endpoints (query1/query2). En
# réutilisant cette session pour nos propres appels groupés à l'API "quote",
# on évite les 401 silencieux qu'on aurait avec un simple `requests.get()`
# "nu" — c'est ce qui, sans ça, fait retomber TOUT le lot sur le repli
# individuel (lent) et explique un affichage qui traîne sur les gros indices.
try:
    from yfinance.data import YfData
    _YF_SESSION = YfData()
except Exception:
    _YF_SESSION = None

conn = st.connection("gsheets", type=GSheetsConnection)

# =======================================================================
# FONCTIONS NEWS
# =======================================================================

@st.cache_data(ttl=900)
def get_quick_news(ticker, nom=None):
    news_list = []
    t_clean = ticker.split('.')[0].strip().upper()

    # Terme de recherche : on privilégie le NOM réel de la société quand il est disponible,
    # car le code ticker seul (ex. "MC", "OR", "V", "T"...) est souvent trop court ou ambigu
    # pour une recherche plein texte sur Google News — il ne renvoie alors que peu ou pas
    # d'articles pertinents, et l'assistant IA se retrouve sans vraies actualités en contexte
    # (d'où des réponses vagues). On retire les suffixes juridiques qui polluent la requête.
    terme_requete = t_clean
    if nom and nom.strip() and nom.strip().upper() != t_clean:
        nom_nettoye = re.sub(
            r'\b(S\.?A\.?|SE|PLC|N\.?V\.?|AG|INC\.?|CORP(?:ORATION)?|CO\.?|LTD\.?|LLC|GROUP|GROUPE)\b\.?',
            '', nom, flags=re.IGNORECASE
        ).strip(' ,.-')
        if len(nom_nettoye) > 2:
            terme_requete = nom_nettoye

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

    def fetch_google_fr(terme):
        q = urllib.parse.quote(f"{terme} bourse")
        url = f"https://news.google.com/rss/search?q={q}&hl=fr&gl=FR&ceid=FR:fr"
        return process_general_google(url, "🇫🇷")

    def fetch_google_us(terme):
        q = urllib.parse.quote(f"{terme} stock news")
        url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
        return process_general_google(url, "🌐")

    def fetch_google_agencies(terme):
        q = urllib.parse.quote(f"{terme} source:Bloomberg OR source:Reuters")
        url = f"https://news.google.com/rss/search?q={q}&hl=en-US"
        return process_general_google(url, badge_icon="💎")

    def fetch_google_wires(terme):
        q = urllib.parse.quote(f"{terme} source:PR_Newswire OR source:Business_Wire")
        url = f"https://news.google.com/rss/search?q={q}&hl=en-US"
        return process_general_google(url, badge_icon="📄", limit=20)

    def fetch_benzinga_fixed(terme):
        url = "https://www.benzinga.com/markets/feed"
        return process_general_google(url, "⚡ Benzinga", default_source="Benzinga")

    def fetch_seeking(terme):
        # Seeking Alpha a besoin du VRAI symbole boursier dans son URL, jamais du nom de la
        # société : on utilise volontairement t_clean (fermeture sur la variable externe) et
        # on ignore le nom passé en argument, contrairement aux autres sources ci-dessus.
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
        futures = [executor.submit(task, terme_requete) for task in tasks]
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


def _decouper_pour_mymemory(texte, taille_max=480):
    """Découpe le texte en morceaux <= taille_max caractères sans couper une
    phrase en deux quand c'est évitable (l'API MyMemory anonyme limite la
    taille d'une requête à ~500 caractères)."""
    phrases = re.split(r'(?<=[.!?])\s+', texte)
    morceaux, courant = [], ""
    for phrase in phrases:
        if len(courant) + len(phrase) + 1 <= taille_max:
            courant = f"{courant} {phrase}".strip()
        else:
            if courant:
                morceaux.append(courant)
            courant = phrase[:taille_max]
    if courant:
        morceaux.append(courant)
    return morceaux


def _traduction_invalide(resultat, texte_source):
    # Certains moteurs ne lèvent pas d'exception quand ils échouent côté
    # serveur : il arrive qu'ils renvoient le contenu d'une page d'erreur
    # HTML ("Error 500...") comme si c'était une traduction valide.
    return (
        not resultat
        or "Error 500" in resultat
        or "That's an error" in resultat
        or "Server Error" in resultat
        or len(resultat) < len(texte_source) * 0.3
    )


@st.cache_data(ttl=3600)
def safe_translate(text):
    if not text or len(text) < 5:
        return text

    # 1) Moteur principal : Google Translate (via deep_translator)
    try:
        resultat = GoogleTranslator(source='auto', target='fr').translate(text)
        if not _traduction_invalide(resultat, text):
            return resultat
        logging.getLogger("streamlit").warning(
            "safe_translate: GoogleTranslator a renvoyé une réponse invalide (page d'erreur ?)"
        )
    except Exception as e:
        logging.getLogger("streamlit").warning(f"safe_translate: GoogleTranslator a échoué : {e}")

    # 2) Repli : moteur MyMemory — API différente de celle de Google, souvent
    # encore accessible quand l'endpoint de scraping de Google Translate est
    # bloqué/rate-limité depuis les IP des plateformes cloud (Streamlit Cloud).
    try:
        morceaux = _decouper_pour_mymemory(text)
        traductions = [
            MyMemoryTranslator(source='en-GB', target='fr-FR').translate(m)
            for m in morceaux
        ]
        resultat = " ".join(t for t in traductions if t)
        if not _traduction_invalide(resultat, text):
            return resultat
        logging.getLogger("streamlit").warning(
            "safe_translate: MyMemoryTranslator a renvoyé une réponse invalide"
        )
    except Exception as e:
        logging.getLogger("streamlit").warning(f"safe_translate: MyMemoryTranslator a échoué : {e}")

    # 3) Aucun des deux moteurs n'a fonctionné : on affiche le texte original
    # (en anglais) plutôt qu'un message d'erreur.
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
            articles = get_quick_news(t, nom_action)
            if articles:
                for a in articles:
                    st.markdown(f"{a['badge']} | **{a['date']}** | [{a['titre']}]({a['lien']})")
            else:
                st.caption(f"Aucune actualité récente pour {t}.")


@st.fragment(run_every="5m")
def actualite_module(liste_tickers):
    col_search, col_sent, col_trad, col_ref, col_force = st.columns([0.4, 0.2, 0.15, 0.1, 0.15])
    with col_search:
        query = st.text_input(
            "🔍 Rechercher...",
            placeholder="Action, mot-clé...",
            label_visibility="collapsed",
            key="news_search_input").lower().strip()

    with col_trad:
        mode_global_fr = st.toggle("🇫🇷", help="Traduction des titres en français",
                                   value=st.session_state.get('mode_fr', False),
                                   key="mode_fr")

    with col_ref:
        if st.button("🔄", help="Actualiser le flux d'actualités", key="refresh_news_btn"):
            get_quick_news.clear()
            st.rerun(scope="fragment")

    with col_force:
        if st.button("🔁 Tout actualiser", help="Forcer l'actualisation complète des données", key="refresh_force_btn"):
            st.cache_data.clear()
            st.rerun()

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


def calc_per_historique_moyen(ticker_obj, years=5):
    """
    PER moyen historique "normalisé", robuste à une année de BNA quasi nul et aux
    incohérences de splits.

    Méthode :
      1. Pour chaque exercice, prix = moyenne des clôtures sur une fenêtre de +/-10 jours
         de bourse autour de la date de fin d'exercice (plutôt qu'un seul jour, pour lisser
         un pic/krach ponctuel ce jour précis).
      2. Prix NON ajustés des splits (auto_adjust=False) : les BNA historiques de Yahoo ne
         sont pas rétroactivement ajustés des splits ultérieurs, contrairement aux prix
         ajustés renvoyés par défaut par yfinance. Utiliser des prix ajustés alors que le
         BNA ne l'est pas fausse complètement le PER pour toute valeur ayant fait un split
         depuis l'exercice concerné.
      3. Exercices écartés si leur BNA s'éloigne trop (facteur x3) de la médiane des BNA
         des exercices disponibles -> ignore une année exceptionnelle (résultat quasi nul
         ou gain one-off).
      4. PER final = (somme des prix retenus) / (somme des BNA retenus), et NON la moyenne
         des PER individuels. Cette pondération par le BNA ("normalisation façon Graham")
         est beaucoup plus robuste qu'une moyenne de ratios : une année à BNA quasi nul ne
         peut plus faire exploser le résultat, car son poids dans la somme reste
         proportionnel à son poids réel au lieu d'être démultiplié par une division par un
         nombre proche de zéro.
      5. Garde-fou final : résultat hors de [3 ; 80] -> jugé non représentatif -> None.
    """
    try:
        fin = ticker_obj.financials
        if fin is None or fin.empty:
            return None

        eps_row = None
        for label in ["Diluted EPS", "Basic EPS"]:
            if label in fin.index:
                eps_row = fin.loc[label].dropna()
                break
        if eps_row is None or eps_row.empty:
            return None

        eps_row = eps_row.head(years)
        eps_vals_pos = sorted(v for v in eps_row if v is not None and not pd.isna(v) and v > 0)
        if len(eps_vals_pos) < 2:
            return None
        eps_median = eps_vals_pos[len(eps_vals_pos) // 2]

        pairs = []  # (prix_moyen_fenetre, bna_exercice)
        for date, eps_val in eps_row.items():
            if eps_val is None or pd.isna(eps_val) or eps_val <= 0:
                continue
            # Ecarte les exercices avec un BNA trop atypique (facteur x3 vs médiane)
            if eps_median > 0 and not (eps_median / 3 <= eps_val <= eps_median * 3):
                continue
            try:
                start = (date - timedelta(days=20)).strftime("%Y-%m-%d")
                end   = (date + timedelta(days=20)).strftime("%Y-%m-%d")
                h = ticker_obj.history(start=start, end=end, auto_adjust=False)
                if h.empty:
                    continue
                h.index = h.index.tz_localize(None)
                target = pd.Timestamp(date).tz_localize(None)
                idx_pos = h.index.get_indexer([target], method="nearest")[0]
                lo, hi = max(0, idx_pos - 10), min(len(h), idx_pos + 11)
                avg_price = h['Close'].iloc[lo:hi].mean()
                if avg_price and avg_price > 0 and not pd.isna(avg_price):
                    pairs.append((float(avg_price), float(eps_val)))
            except Exception:
                continue

        if len(pairs) < 2:
            return None

        total_price = sum(p for p, _ in pairs)
        total_eps   = sum(e for _, e in pairs)
        if total_eps <= 0:
            return None

        result = total_price / total_eps

        if result <= 3 or result > 80:
            return None
        return round(result, 2)
    except Exception:
        return None


@st.cache_data(ttl=3600)
def fetch_stock_data(ticker_str):
    yf.set_tz_cache_location("/tmp")
    try:
        s    = yf.Ticker(ticker_str.strip())
        info = s.info
        p    = info.get("currentPrice") or info.get("regularMarketPrice")
        if p is None: return None

        ef, pf = info.get("forwardEps", 0), info.get("forwardPE", 15)
        trailing_eps_raw = info.get("trailingEps") or 0
        trailing_pe_raw  = info.get("trailingPE")

        # PER utilisé pour la valorisation "BNA Forward" : on utilise en priorité le PER
        # Actuel de Yahoo (trailingPE), une donnée unique et fiable, déjà cohérente en
        # échelle/devise avec le prix actuel. Le PER historique reconstruit à partir des
        # données brutes (financials + historique de prix) s'est révélé fragile sur
        # certains titres (dates mal alignées, splits, données ponctuelles aberrantes) et
        # n'est donc utilisé qu'en repli secondaire, si le PER Actuel est indisponible ou
        # hors plage plausible. Dernier repli : PER Forward, puis 15 par défaut.
        per_hist_moy = None
        if trailing_pe_raw and 5 <= trailing_pe_raw <= 60:
            pf_valo = trailing_pe_raw
            per_source = "PER Actuel"
        else:
            per_hist_moy = calc_per_historique_moyen(s, years=5)
            if per_hist_moy and per_hist_moy > 0:
                pf_valo = per_hist_moy
                per_source = "PER Historique Moyen"
            elif pf and 3 < pf <= 80:
                pf_valo = pf
                per_source = "PER Forward (repli)"
            else:
                pf_valo = 15
                per_source = "Défaut 15 (repli)"

        # Pondération BNA Forward / BNA Actuel : le consensus des analystes (BNA Forward)
        # est réputé structurellement optimiste (les études montrent une tendance
        # récurrente à la surestimation de la croissance à 1 an). Pour un "Juste Prix"
        # prudent, on ne prend le consensus qu'à hauteur de 30%, en pondérant à 70% le BNA
        # Actuel réellement constaté. Si le BNA Actuel n'est pas disponible (ex: perte
        # ponctuelle), on retombe sur 100% BNA Forward, faute de mieux.
        BNA_POIDS_FORWARD = 0.30
        BNA_POIDS_ACTUEL  = 0.70
        if trailing_eps_raw and trailing_eps_raw > 0 and ef:
            ef_pondere = (ef * BNA_POIDS_FORWARD) + (trailing_eps_raw * BNA_POIDS_ACTUEL)
        else:
            ef_pondere = ef

        # Plafond de sécurité supplémentaire : même après pondération, si le résultat
        # dépasse le BNA Actuel de plus de 50%, on plafonne à +50% (garde-fou en cas de
        # BNA Actuel anormalement déprimé).
        ef_valo = ef_pondere
        if trailing_eps_raw and trailing_eps_raw > 0 and ef_pondere and ef_pondere > trailing_eps_raw * 1.5:
            ef_valo = trailing_eps_raw * 1.5

        vb      = ef_valo * pf_valo
        tm     = info.get("targetMeanPrice", 0)
        sh     = info.get("sharesOutstanding", 1)

        fcf_raw = s.cashflow.loc["Free Cash Flow"].dropna().head(3).mean() if "Free Cash Flow" in s.cashflow.index else 0
        vf  = (fcf_raw / sh * 1.05) * pf if sh > 0 else 0
        mods = [v for v in [vb, vf, tm] if v > 0]
        avg  = sum(mods) / len(mods) if mods else 0
        p_s, p_d = calculate_piotroski_advanced(s)

        current_year = datetime.now().year
        hist = s.history(start=f"{current_year}-01-01", auto_adjust=False)

        perf_1j = perf_1m = perf_ytd = perf_ytd_total = 0
        if len(hist) >= 2:
            c_veille      = hist['Close'].iloc[-2]
            c_debut_annee = hist['Close'].iloc[0]
            perf_1j  = ((p - c_veille) / c_veille) * 100
            perf_ytd = ((p - c_debut_annee) / c_debut_annee) * 100

            # Rendement total YTD = variation de cours + dividendes détachés depuis le 1er janvier
            try:
                div_series = s.dividends
                if div_series is not None and not div_series.empty:
                    start_ts = pd.Timestamp(f"{current_year}-01-01")
                    if div_series.index.tz is not None:
                        start_ts = start_ts.tz_localize(div_series.index.tz)
                    dividends_ytd = float(div_series[div_series.index >= start_ts].sum())
                else:
                    dividends_ytd = 0.0
            except Exception:
                dividends_ytd = 0.0
            perf_ytd_total = ((p + dividends_ytd - c_debut_annee) / c_debut_annee) * 100

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
            "Source PER (BNA)": per_source,
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
            "Chg 1J": perf_1j,
            "Chg YTD": perf_ytd,
            "Chg YTD (div. incl.)": perf_ytd_total,
            "Chg 1M": perf_1m,
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
                "eps_fwd": ef, "eps_fwd_valo": ef_valo, "per_fwd": pf, "per_valo_bna": pf_valo,
                "per_hist_moy": per_hist_moy, "per_source": per_source,
                "fcf_ps": fcf_raw / sh if sh > 0 else 0,
                "num_analysts": info.get("numberOfAnalystOpinions", 0)
            }
        }
    except:
        return None


# =======================================================================
# IA — VERDICT D'INVESTISSEMENT (basé uniquement sur les données réelles
# déjà récupérées par fetch_stock_data : aucune clé API, aucun appel réseau
# supplémentaire. Logique transparente et déterministe, pas une boîte noire.)
# =======================================================================

def _parse_num(v):
    """Convertit une valeur (float, ou string formatée type '12.3%' / '4.5x' / '+8.1%')
    en float, ou None si non exploitable."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return None if pd.isna(v) else float(v)
    s = str(v).strip()
    if s in ("N/A", "", "None", "nan"):
        return None
    s = s.replace('%', '').replace('x', '').replace('+', '')
    try:
        return float(s)
    except Exception:
        return None


def analyser_action_ia(d):
    """Génère un verdict d'investissement (score + explications) à partir des
    fondamentaux réels déjà calculés par fetch_stock_data pour ce titre :
    valorisation (juste prix de synthèse), santé financière (Piotroski),
    rentabilité (ROE/ROA/marge), endettement, croissance (CAGR/EBITDA),
    consensus analystes, dividende et momentum récent.

    Retourne (verdict: str, couleur_hex: str, score: int, bullets: list[(icone, texte)])
    """
    fd = d.get('full_data', {}) or {}
    score = 0
    bullets = []

    # --- 1. Valorisation vs "juste prix" de synthèse (moyenne BNA / FCF / analystes) ---
    prix = d.get('Prix Actuel')
    juste_prix = fd.get('fair_avg')
    if prix and juste_prix and juste_prix > 0:
        ecart = (prix - juste_prix) / juste_prix * 100
        devise = fd.get('currency', '')
        if ecart <= -15:
            score += 2
            bullets.append(("🟢", f"Fortement sous-valorisée : le cours ({clean_num(prix)}) est {abs(ecart):.0f}% sous le juste prix de synthèse estimé ({clean_num(juste_prix)} {devise})."))
        elif ecart <= 0:
            score += 1
            bullets.append(("🟢", f"Sous-valorisée : {abs(ecart):.0f}% sous le juste prix de synthèse estimé ({clean_num(juste_prix)} {devise})."))
        elif ecart <= 15:
            bullets.append(("🟡", f"Proche du juste prix de synthèse estimé (cours {ecart:+.0f}% par rapport à {clean_num(juste_prix)} {devise})."))
        else:
            score -= 1
            bullets.append(("🔴", f"Se traite {ecart:+.0f}% au-dessus du juste prix de synthèse estimé — risque de correction si la croissance attendue n'est pas au rendez-vous."))
    else:
        bullets.append(("⚪", "Juste prix non calculable (données de valorisation insuffisantes pour ce titre)."))

    # --- 2. Santé financière (score de Piotroski, 0 à 5) ---
    piotroski = d.get('Santé (Piotroski)', 'N/A')
    if '/' in str(piotroski):
        try:
            num = int(str(piotroski).split('/')[0])
            if num >= 4:
                score += 1
                bullets.append(("🟢", f"Santé financière solide (score de Piotroski {piotroski})."))
            elif num <= 2:
                score -= 1
                bullets.append(("🔴", f"Santé financière fragile (score de Piotroski {piotroski}) — vigilance sur la rentabilité et le cash-flow."))
            else:
                bullets.append(("🟡", f"Santé financière correcte mais perfectible (score de Piotroski {piotroski})."))
        except Exception:
            pass

    # --- 3. Rentabilité (ROE, ROA, marge nette) ---
    roe = _parse_num(d.get('ROE'))
    roa = _parse_num(d.get('ROA'))
    marge = _parse_num(d.get('Marge Nette'))
    signaux_rentab = [v for v in [roe, roa, marge] if v is not None]
    if signaux_rentab:
        ok = sum(1 for v, seuil in [(roe, 15), (roa, 8), (marge, 15)] if v is not None and v >= seuil)
        if ok >= 2:
            score += 1
            bullets.append(("🟢", f"Bonne rentabilité (ROE {d.get('ROE')}, ROA {d.get('ROA')}, marge nette {d.get('Marge Nette')})."))
        elif ok == 0:
            score -= 1
            bullets.append(("🔴", f"Rentabilité faible (ROE {d.get('ROE')}, ROA {d.get('ROA')}, marge nette {d.get('Marge Nette')})."))
        else:
            bullets.append(("🟡", f"Rentabilité moyenne (ROE {d.get('ROE')}, ROA {d.get('ROA')}, marge nette {d.get('Marge Nette')})."))

    # --- 4. Endettement (Dette/Equity) ---
    dette = _parse_num(d.get('Dette/Equity'))
    if dette is not None:
        if dette < 50:
            score += 1
            bullets.append(("🟢", f"Endettement maîtrisé (Dette/Equity {d.get('Dette/Equity')})."))
        elif dette > 150:
            score -= 1
            bullets.append(("🔴", f"Endettement élevé (Dette/Equity {d.get('Dette/Equity')}) — sensibilité accrue à la hausse des taux."))
        else:
            bullets.append(("🟡", f"Endettement modéré (Dette/Equity {d.get('Dette/Equity')})."))

    # --- 5. Croissance (CAGR 3 ans + croissance EBITDA) ---
    cagr3 = _parse_num(d.get('CAGR 3 ans'))
    ebitda_g = _parse_num(d.get('Croissance EBITDA'))
    signaux_croissance = [v for v in [cagr3, ebitda_g] if v is not None]
    if signaux_croissance:
        moyenne = sum(signaux_croissance) / len(signaux_croissance)
        if moyenne > 10:
            score += 1
            bullets.append(("🟢", f"Dynamique de croissance solide (CAGR 3 ans {d.get('CAGR 3 ans')}, croissance EBITDA {d.get('Croissance EBITDA')})."))
        elif moyenne < 0:
            score -= 1
            bullets.append(("🔴", f"Croissance en repli (CAGR 3 ans {d.get('CAGR 3 ans')}, croissance EBITDA {d.get('Croissance EBITDA')})."))
        else:
            bullets.append(("🟡", f"Croissance modérée (CAGR 3 ans {d.get('CAGR 3 ans')})."))

    # --- 6. Consensus des analystes ---
    avis = d.get('Avis Analystes', 'N/A') or 'N/A'
    if 'Achat' in avis:
        score += 1
        bullets.append(("🟢", f"Consensus analystes favorable ({avis}, sur {fd.get('num_analysts', 0)} opinions)."))
    elif 'Vendre' in avis or 'Alléger' in avis:
        score -= 1
        bullets.append(("🔴", f"Consensus analystes défavorable ({avis})."))

    # --- 7. Dividende (information, non pondéré) ---
    rendement = d.get('Rendement %', 0) or 0
    if rendement > 0:
        bullets.append(("💰", f"Verse un dividende avec un rendement de {rendement}%."))

    # --- 8. Momentum court terme (information, non pondéré fortement) ---
    chg_1m = d.get('Chg 1M')
    if chg_1m is not None and not (isinstance(chg_1m, float) and pd.isna(chg_1m)):
        if chg_1m <= -15:
            bullets.append(("⚠️", f"Fort repli récent ({chg_1m:+.1f}% sur 1 mois) — à recouper avec l'actualité avant toute décision."))
        elif chg_1m >= 20:
            bullets.append(("⚠️", f"Forte hausse récente ({chg_1m:+.1f}% sur 1 mois) — vérifier que la valorisation n'a pas trop couru par rapport aux fondamentaux."))

    # --- Verdict final ---
    if score >= 4:
        verdict, couleur = "🟢 ACHAT FORT", "#1e7e34"
    elif score >= 2:
        verdict, couleur = "🟢 ACHAT", "#28a745"
    elif score >= 0:
        verdict, couleur = "🟡 CONSERVER / SURVEILLER", "#e8a300"
    elif score >= -2:
        verdict, couleur = "🟠 PRUDENCE", "#fd7e14"
    else:
        verdict, couleur = "🔴 RISQUE ÉLEVÉ", "#dc3545"

    return verdict, couleur, score, bullets


@st.cache_data(ttl=86400)
def get_company_profile(ticker):
    """Profil qualitatif de la société (description, secteur, industrie, pays,
    effectifs, site web) — un seul appel réseau, mis en cache 24h."""
    try:
        info = yf.Ticker(ticker).info
        resume = info.get('longBusinessSummary', '') or ''
        return {
            'resume': resume,
            'resume_fr': safe_translate(resume) if resume else '',
            'secteur': info.get('sector', 'N/A'),
            'industrie': info.get('industry', 'N/A'),
            'pays': info.get('country', 'N/A'),
            'employes': info.get('fullTimeEmployees'),
            'site': info.get('website', ''),
        }
    except Exception:
        return {}


# =======================================================================
# DONNÉES COMPLÉMENTAIRES — GOOGLE FINANCE (best-effort)
# Google Finance n'a pas d'API publique officielle : on récupère les
# indicateurs affichés sur la page publique google.com/finance/quote/...
# via scraping HTML. Cette page n'est pas documentée/stable dans le temps
# (classes CSS générées, structure pouvant changer sans préavis), donc
# cette fonction est volontairement défensive : en cas d'échec (page
# modifiée, ticker introuvable, blocage), elle retourne un dict vide
# plutôt que de faire planter l'app — exactement comme le repli déjà en
# place pour les échecs Yahoo Finance plus haut dans ce fichier.
# =======================================================================

# Correspondance suffixe Yahoo Finance -> code de place boursière Google Finance.
# Couvre les principales places européennes ciblées par l'app ; à compléter
# au besoin si de nouveaux marchés sont ajoutés.
_SUFFIXE_YF_VERS_BOURSE_GOOGLE = {
    ".PA": "EPA",   # Euronext Paris
    ".AS": "AMS",   # Euronext Amsterdam
    ".BR": "EBR",   # Euronext Brussels
    ".LS": "ELI",   # Euronext Lisbon
    ".DE": "ETR",   # Deutsche Börse Xetra
    ".MI": "BIT",   # Borsa Italiana Milan
    ".MC": "BME",   # Bolsa de Madrid
    ".L": "LON",    # London Stock Exchange
    ".SW": "VTX",   # SIX Swiss Exchange
}


def _ticker_yahoo_vers_google(ticker):
    """Convertit un ticker au format Yahoo Finance (ex: 'MC.PA', 'AAPL') vers le
    format attendu par Google Finance (ex: 'MC:EPA', 'AAPL:NASDAQ'). Retourne
    None si le suffixe de place boursière n'est pas reconnu (mieux vaut ne pas
    interroger Google Finance que de deviner une bourse au hasard)."""
    t = ticker.strip().upper()
    for suffixe, bourse in _SUFFIXE_YF_VERS_BOURSE_GOOGLE.items():
        if t.endswith(suffixe):
            symbole = t[: -len(suffixe)]
            return f"{symbole}:{bourse}"
    if "." not in t:
        # Pas de suffixe : on suppose une valeur US, en tentant NASDAQ puis NYSE.
        return f"{t}:NASDAQ"
    return None


@st.cache_data(ttl=3600, show_spinner=False)
def get_google_finance_data(ticker):
    """Récupère quelques indicateurs publiés sur la fiche Google Finance du titre
    (best-effort, voir note ci-dessus). Retourne un dict (éventuellement vide) de
    libellé -> valeur tels qu'affichés sur la page, sans recalcul ni interprétation."""
    symbole_google = _ticker_yahoo_vers_google(ticker)
    if not symbole_google:
        return {}

    candidats = [symbole_google]
    if symbole_google.endswith(":NASDAQ"):
        candidats.append(symbole_google.replace(":NASDAQ", ":NYSE"))

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    for candidat in candidats:
        try:
            url = f"https://www.google.com/finance/quote/{candidat}"
            r = requests.get(url, headers=headers, timeout=6)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "html.parser")

            donnees = {}
            # Prix actuel affiché en tête de page.
            prix_el = soup.select_one("div.YMlKec.fxKbKc")
            if prix_el:
                donnees["Cours (Google Finance)"] = prix_el.get_text(strip=True)

            # Bloc "A propos" : paires libellé/valeur (capitalisation, P/E, rendement, etc.)
            for ligne in soup.select("div.gyFHrc"):
                libelle_el = ligne.select_one("div.mfs7Fc")
                valeur_el = ligne.select_one("div.P6K39c")
                if libelle_el and valeur_el:
                    libelle = libelle_el.get_text(strip=True)
                    valeur = valeur_el.get_text(strip=True)
                    if libelle and valeur:
                        donnees[libelle] = valeur

            if donnees:
                return donnees
        except Exception:
            continue

    return {}


# =======================================================================
# CONNEXION À UN VRAI LLM (GPT-OSS 120B, gratuit, via OpenRouter)
# Le modèle ne reçoit QUE les signaux déjà calculés par analyser_action_ia
# (chiffres réels) — il ne fait qu'expliquer/mettre en perspective, il ne
# recalcule ni n'invente aucune donnée financière.
# =======================================================================

OPENROUTER_MODELE_GRATUIT = "openai/gpt-oss-120b"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Liste des modèles sélectionnables par l'utilisateur (id OpenRouter -> libellé affiché).
# Snapshot des modèles ":free" d'OpenRouter les plus pertinents pour l'analyse financière/
# actions. À vérifier/actualiser de temps en temps sur https://openrouter.ai/models, la
# liste des modèles gratuits tourne souvent.
MODELES_LLM_DISPONIBLES = {
    "openrouter/free": "🔀 Free Models Router (auto — bascule seule si saturé)",
    "openai/gpt-oss-120b": "GPT-OSS 120B (OpenAI)",
    "nvidia/nemotron-3-ultra-550b-a55b:free": "Nemotron 3 Ultra 550B (NVIDIA — le plus rigoureux)",
    "nvidia/nemotron-3-super-120b-a12b:free": "Nemotron 3 Super 120B (NVIDIA)",
    "nvidia/nemotron-3.5-lightning:free": "Nemotron 3.5 Lightning (NVIDIA — rapide)",
    "nvidia/nemotron-nano-9b-v2:free": "Nemotron Nano 9B (NVIDIA — léger/rapide)",
    "z-ai/glm-5.2:free": "GLM 5.2 (Z.ai — bon suivi d'instructions)",
    "google/gemma-4-26b-a4b-it:free": "Gemma 4 26B (Google — bon en chat)",
}


def selecteur_modele_llm(key_suffix=""):
    """Affiche un selectbox (modèle) + un slider (température) permettant de configurer les
    appels OpenRouter (analyse détaillée + chat). Les choix sont mémorisés dans
    st.session_state sous des clés globales ('modele_llm_choisi', 'temperature_llm_choisie')
    afin de rester les mêmes pour toute l'application tant que l'utilisateur ne les change pas
    explicitement. Retourne (modele_id, temperature)."""
    cle_modele = "modele_llm_choisi"
    cle_temp = "temperature_llm_choisie"
    if cle_modele not in st.session_state:
        st.session_state[cle_modele] = OPENROUTER_MODELE_GRATUIT
    if cle_temp not in st.session_state:
        st.session_state[cle_temp] = 0.2  # bas par défaut : réponses factuelles, peu créatives

    st.markdown(
        """
        <style>
        /* Sensibilité IA : composant discret, teinte neutre (plus de rouge), sans hack de mise
           en page qui casse le rendu — un filtre de désaturation s'applique à tout le composant
           quelle que soit la structure interne du widget, donc rien ne "casse" visuellement. */
        div[data-testid="stSlider"] {
            filter: grayscale(1) opacity(0.8);
            transition: filter 0.15s ease;
            padding-top: 0.15rem;
        }
        div[data-testid="stSlider"]:hover,
        div[data-testid="stSlider"]:focus-within {
            filter: grayscale(0.35) opacity(1);
        }
        div[data-testid="stSlider"] label p {
            font-size: 0.8rem !important;
            color: #888 !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    col_modele, col_temp = st.columns([3, 1])
    with col_modele:
        ids_modeles = list(MODELES_LLM_DISPONIBLES.keys())
        valeur_actuelle = st.session_state[cle_modele]
        index_defaut = ids_modeles.index(valeur_actuelle) if valeur_actuelle in ids_modeles else 0
        choix_modele = st.selectbox(
            "🧠 Modèle IA utilisé",
            options=ids_modeles,
            index=index_defaut,
            format_func=lambda mid: MODELES_LLM_DISPONIBLES.get(mid, mid),
            key=f"modele_llm_selectbox{key_suffix}",
            help="S'applique à l'analyse rapide, l'analyse détaillée et l'assistant conversationnel. "
                 "« Free Models Router » choisit lui-même un modèle gratuit disponible et bascule "
                 "automatiquement en cas de saturation — pratique pour éviter de tester chaque "
                 "modèle à la main. Les modèles nommés individuellement ont des limites de débit "
                 "(typiquement ~20 req/min, ~200 req/jour par compte OpenRouter) : change de "
                 "modèle si l'un d'eux est rate-limité ou répond mal.",
        )
        st.session_state[cle_modele] = choix_modele
    with col_temp:
        choix_temp = st.slider(
            "Sensibilité IA",
            min_value=0.0,
            max_value=1.0,
            value=st.session_state[cle_temp],
            step=0.1,
            key=f"temperature_llm_slider{key_suffix}",
            help="Basse (0.1–0.3) = réponses factuelles et rigoureuses, recommandé pour la "
                 "finance. Haute = réponses plus créatives/variées, au risque d'inventer des "
                 "détails. Reste basse sauf besoin spécifique.",
        )
        st.session_state[cle_temp] = choix_temp

    return choix_modele, choix_temp


def afficher_erreur_llm(message_erreur):
    """Affiche une erreur d'appel LLM de façon lisible : message clair et actionnable pour les
    cas fréquents (429 = quota/débit du modèle gratuit épuisé côté fournisseur upstream, 401 =
    clé invalide), détail brut replié dans un expander pour le débogage."""
    if "429" in message_erreur:
        st.warning(
            "⏳ Ce modèle est temporairement saturé (trop de demandes chez le "
            "fournisseur en ce moment). Change de modèle dans le sélecteur ci-dessus, ou "
            "réessaie dans quelques instants."
        )
    elif "401" in message_erreur:
        st.warning("🔑 Clé API OpenRouter invalide ou expirée. Vérifie-la dans tes secrets.")
    else:
        st.warning("⚠️ Erreur lors de l'appel au LLM. Essaie un autre modèle ou réessaie dans quelques instants.")
    with st.expander("Détail technique de l'erreur"):
        st.code(message_erreur)


def _get_openrouter_api_key():
    """Cherche la clé API OpenRouter dans st.secrets puis dans l'environnement.
    Même pour le modèle gratuit, OpenRouter exige une clé (création de compte gratuite
    sur https://openrouter.ai/keys) — seule la consommation de tokens ne coûte rien."""
    try:
        if "OPENROUTER_API_KEY" in st.secrets:
            return st.secrets["OPENROUTER_API_KEY"]
    except Exception:
        pass
    return os.environ.get("OPENROUTER_API_KEY")


def _appel_openrouter(system_prompt, user_message, model=OPENROUTER_MODELE_GRATUIT, temperature=0.2,
                       max_tokens=2200):
    """Appelle un modèle via l'API OpenRouter (compatible OpenAI) pour un échange à un seul
    tour (system + 1 message utilisateur). Retourne le texte, None si pas de clé, ou
    '__ERROR__:...' en cas d'échec."""
    return _appel_openrouter_messages(
        [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_message}],
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
    )


def _requete_openrouter(payload, api_key):
    """Effectue l'appel HTTP brut vers OpenRouter et retourne (texte, code_http, corps_brut)."""
    r = requests.post(
        OPENROUTER_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            # Recommandé par OpenRouter, sans impact fonctionnel si laissé générique :
            "HTTP-Referer": "https://streamlit.io",
            "X-Title": "Assistant Boursier IA",
        },
        json=payload,
        timeout=45,
    )
    return r


def _appel_openrouter_messages(messages, model=OPENROUTER_MODELE_GRATUIT, web_search=False, temperature=0.2,
                                max_tokens=2200, _relance=0):
    """Appelle un modèle via l'API OpenRouter (compatible OpenAI) avec un historique complet
    de messages (utilisé pour le chat multi-tours). `messages` est une liste de dicts
    {"role": "system"|"user"|"assistant", "content": str}.

    `temperature` : fixée basse par défaut (0.2) pour privilégier des réponses factuelles et
    rigoureuses plutôt que créatives — recommandé pour de l'analyse financière (moins de dérive
    et d'« hallucination » de chiffres).

    `max_tokens` : 2200 par défaut. Certains modèles gratuits (notamment ceux avec un
    raisonnement interne caché) consomment une partie de ce budget en « réflexion » avant même
    de produire la réponse visible, ce qui peut couper la réponse avant la fin même avec une
    limite généreuse. Pour compenser, si l'API indique `finish_reason == "length"` (réponse
    coupée faute de budget), on relance automatiquement 1 à 2 appels de continuation (voir
    `_relance`) en demandant au modèle de reprendre exactement où il s'est arrêté, puis on
    concatène les morceaux — plutôt que de renvoyer une réponse tronquée à l'utilisateur.

    `web_search` : active la recherche web en direct via le server tool moderne d'OpenRouter
    (`tools: [{"type": "openrouter:web_search"}]`), qui remplace l'ancien plugin `plugins:
    [{"id": "web"}]` (désormais déprécié côté OpenRouter). Ce server tool exige un modèle
    supportant le tool-calling ; si l'appel échoue pour cette raison (400, message mentionnant
    "tool"/"function"), on retente automatiquement avec l'ancien plugin `web`, qui fonctionne
    quel que soit le modèle (il injecte simplement les résultats en contexte, sans tool-calling).

    Retourne le texte, None si pas de clé, ou '__ERROR__:...' en cas d'échec."""
    api_key = _get_openrouter_api_key()
    if not api_key:
        return None
    try:
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if web_search:
            payload["tools"] = [{"type": "openrouter:web_search"}]

        r = _requete_openrouter(payload, api_key)

        # Repli sur l'ancien plugin 'web' si le server tool moderne échoue parce que le modèle
        # ne supporte pas le tool-calling (erreur 400 mentionnant tool/function), plutôt que de
        # renvoyer une erreur sèche à l'utilisateur pour un modèle qui, sinon, fonctionne bien.
        if web_search and r.status_code == 400 and re.search(r"tool|function", r.text, re.IGNORECASE):
            payload.pop("tools", None)
            payload["plugins"] = [{"id": "web"}]
            r = _requete_openrouter(payload, api_key)

        if r.status_code != 200:
            return f"__ERROR__:HTTP {r.status_code} — {r.text[:200]}"
        data = r.json()
        choix = data["choices"][0]
        contenu = choix["message"]["content"]
        if not contenu:
            return "__ERROR__:réponse vide du modèle."
        contenu = contenu.strip()

        # Réponse coupée faute de budget de tokens : on relance une continuation (max 2 fois)
        # en donnant au modèle sa propre réponse partielle comme contexte, plutôt que de
        # renvoyer un texte tronqué en plein milieu d'une phrase ou d'une section.
        if choix.get("finish_reason") == "length" and _relance < 2:
            messages_suite = messages + [
                {"role": "assistant", "content": contenu},
                {"role": "user", "content": (
                    "Continue exactement là où tu t'es arrêté, sans répéter ce qui précède et "
                    "sans réintroduire de préambule. Termine la réponse."
                )},
            ]
            suite = _appel_openrouter_messages(
                messages_suite, model=model, web_search=False, temperature=temperature,
                max_tokens=max_tokens, _relance=_relance + 1,
            )
            if suite and not suite.startswith("__ERROR__:"):
                contenu = contenu + "\n" + suite

        return contenu
    except Exception as e:
        return f"__ERROR__:{e}"


@st.cache_data(ttl=3600, show_spinner=False)
def generer_analyse_llm(ticker, nom, secteur, verdict_ia, score_ia, bullets_txt, profil_resume,
                         news_txt=(), google_data=None, model=OPENROUTER_MODELE_GRATUIT, temperature=0.2):
    """Demande à un LLM via OpenRouter (modèle choisi par l'utilisateur, gratuit par défaut) de
    rédiger une synthèse en langage naturel à partir des signaux fondamentaux déjà calculés
    (bullets_txt, issus de Yahoo Finance), des indicateurs complémentaires trouvés sur Google
    Finance (google_data, best-effort) et des actualités récentes déjà collectées (news_txt,
    via get_quick_news). Mis en cache 1h par (titre, modèle) pour limiter le débit. Retourne
    le texte, None si pas de clé API, ou '__ERROR__:...' en cas d'échec de l'appel."""
    contexte = "\n".join(f"- {b}" for b in bullets_txt)
    contexte_news = "\n".join(f"- {n}" for n in news_txt)
    contexte_google = "\n".join(f"- {libelle} : {valeur}" for libelle, valeur in (google_data or {}).items())
    system_prompt = (
        "Tu es un analyste financier pédagogue qui écrit en français pour un investisseur "
        "particulier. On te fournit une liste de signaux fondamentaux déjà calculés à partir "
        "de données réelles (Yahoo Finance) : valorisation, santé financière, rentabilité, "
        "endettement, croissance, consensus analystes — éventuellement complétés par quelques "
        "indicateurs relevés sur Google Finance, ainsi qu'une sélection des actualités les plus "
        "récentes sur le titre. Ta tâche est de mettre ces signaux en perspective, d'expliquer "
        "ce qu'ils signifient concrètement, de relier si pertinent l'actualité récente aux "
        "fondamentaux (ex : une actualité négative qui confirme ou contredit un signal), de "
        "signaler explicitement toute divergence notable entre Yahoo Finance et Google Finance "
        "si tu en observes une, de souligner les points de vigilance et les incertitudes, et de "
        "conclure par une synthèse équilibrée. N'invente JAMAIS de chiffre ou d'événement qui ne "
        "t'a pas été fourni. Reste concis (4 à 6 paragraphes courts ou puces). Termine toujours "
        "par une phrase rappelant que ceci n'est pas un conseil en investissement personnalisé."
    )
    user_message = (
        f"Société : {nom} ({ticker})\n"
        f"Secteur : {secteur}\n"
        f"Verdict automatique (score {score_ia:+d}) : {verdict_ia}\n\n"
        f"Signaux fondamentaux détectés (Yahoo Finance) :\n{contexte}\n\n"
        + (f"Indicateurs complémentaires (Google Finance) :\n{contexte_google}\n\n" if contexte_google else "")
        + (f"Activité de l'entreprise : {profil_resume}\n\n" if profil_resume else "")
        + (f"Actualités récentes (titre, source, date) :\n{contexte_news}\n\n" if contexte_news else "")
        + "Rédige ton analyse maintenant."
    )

    return _appel_openrouter(system_prompt, user_message, model=model, temperature=temperature)


def extraire_texte_rapport_uploade(fichier_uploade, max_caracteres=18000):
    """Extrait le texte d'un rapport de résultats uploadé par l'utilisateur (PDF ou .txt),
    pour qu'un LLM puisse l'analyser à la place/en complément des actualités. Tronqué à
    `max_caracteres` pour rester dans un budget de tokens raisonnable (le début et la fin d'un
    rapport — chiffres clés et perspectives — sont généralement les sections les plus denses en
    information, donc on garde les deux extrémités plutôt que de couper net à la fin).
    Retourne (texte, message_erreur) : l'un des deux est toujours None."""
    if fichier_uploade is None:
        return None, None
    nom_fichier = fichier_uploade.name.lower()
    try:
        if nom_fichier.endswith(".pdf"):
            if not _PYPDF_DISPONIBLE:
                return None, "Lecture PDF indisponible sur ce serveur (dépendance 'pypdf' manquante — ajoute-la à requirements.txt)."
            lecteur = PdfReader(fichier_uploade)
            texte = "\n".join((page.extract_text() or "") for page in lecteur.pages)
        elif nom_fichier.endswith(".txt"):
            texte = fichier_uploade.read().decode("utf-8", errors="ignore")
        else:
            return None, "Format non supporté : uploade un PDF ou un fichier .txt."
        texte = texte.strip()
        if not texte:
            return None, "Aucun texte exploitable trouvé dans ce fichier (PDF scanné/image sans OCR ?)."
        if len(texte) > max_caracteres:
            moitie = max_caracteres // 2
            texte = (
                texte[:moitie]
                + "\n\n[...contenu tronqué pour respecter la limite de contexte...]\n\n"
                + texte[-moitie:]
            )
        return texte, None
    except Exception as e:
        return None, f"Erreur lors de la lecture du fichier : {e}"


# Bibliothèque de prompts d'« analyse détaillée » façon recherche actions professionnelle.
# Chaque template est complété automatiquement avec le ticker, le nom et le secteur de
# l'action sélectionnée (plus de saisie manuelle de [TICKER]/[COMPANY]).
PROMPTS_ANALYSE_DETAILLEE = {
    "wallstreet": {
        "label": "1️⃣ Analyse boursière complète (style Wall Street)",
        "template": (
            "Fais comme si tu étais un analyste senior en recherche actions de Wall Street. "
            "Analyse l'action {nom} ({ticker}).\n\nInclus :\n"
            "• Modèle économique et sources de revenus\n"
            "• Avantages concurrentiels (fossé protecteur)\n"
            "• Tendances de l'industrie\n"
            "• Santé financière (croissance des revenus, marges, dette)\n"
            "• Risques clés\n"
            "• Évaluation par rapport aux concurrents\n"
            "• Scénarios haussier, baissier et de base\n"
            "• Perspectives à 12–24 mois\n\n"
            "Explique en termes simples mais avec des insights professionnels."
        ),
    },
    "financiere": {
        "label": "2️⃣ Analyse financière approfondie",
        "template": (
            "Analyse les dernières années de finances disponibles pour {nom} ({ticker}). "
            "Décompose :\n"
            "• Croissance du chiffre d'affaires\n"
            "• Tendances du revenu net\n"
            "• Flux de trésorerie disponible\n"
            "• Marges bénéficiaires\n"
            "• Niveaux d'endettement\n"
            "• Rendement des capitaux propres\n\n"
            "Explique si l'entreprise est financièrement solide ou en affaiblissement. Si "
            "l'historique pluriannuel complet n'est pas présent dans les données fournies, "
            "dis-le clairement plutôt que de l'inventer."
        ),
    },
    "fosse": {
        "label": "3️⃣ Analyse de l'avantage concurrentiel (fossé économique)",
        "template": (
            "Évalue le fossé économique concurrentiel de {nom} ({ticker}). Discute :\n"
            "• Force de la marque\n"
            "• Effets de réseau\n"
            "• Coûts de changement\n"
            "• Avantage en coûts\n"
            "• Brevets ou technologie propriétaire\n\n"
            "Compare avec les principaux concurrents du secteur ({secteur}) et évalue le "
            "fossé sur une échelle de 1 à 10."
        ),
    },
    "evaluation": {
        "label": "4️⃣ Évaluation de l'action (comme une banque d'investissement)",
        "template": (
            "Effectue une analyse d'évaluation de {nom} ({ticker}), comme le ferait une "
            "banque d'investissement. Inclus :\n"
            "• Comparaison du ratio P/E\n"
            "• Estimation du flux de trésorerie actualisé (DCF), en précisant les hypothèses "
            "retenues\n"
            "• Comparaison avec la moyenne du secteur ({secteur})\n"
            "• Conclusion sur la sous-évaluation ou la surévaluation du titre\n\n"
            "Appuie-toi sur les données de valorisation fournies ci-dessous et signale "
            "clairement les limites de l'exercice si des données manquent."
        ),
    },
    "risques": {
        "label": "5️⃣ Analyse des risques",
        "template": (
            "Identifie les plus grands risques d'investissement dans {nom} ({ticker}). "
            "Inclus :\n"
            "• Risques économiques\n"
            "• Perturbation de l'industrie\n"
            "• Concurrence\n"
            "• Menaces réglementaires\n"
            "• Risques de dette ou financiers\n\n"
            "Classe les risques du plus au moins dangereux."
        ),
    },
    "croissance": {
        "label": "6️⃣ Analyse du potentiel de croissance",
        "template": (
            "Analyse le potentiel de croissance future de {nom} ({ticker}). Considère :\n"
            "• Taille du marché\n"
            "• Taux de croissance du secteur ({secteur})\n"
            "• Opportunités d'expansion\n"
            "• Nouveaux produits\n"
            "• Avantages liés à l'IA ou à la technologie\n\n"
            "Estime la croissance potentielle sur les 5 à 10 prochaines années, en restant "
            "prudent si les données fournies ne permettent pas une projection précise."
        ),
    },
    "resultats": {
        "label": "9️⃣ Analyse détaillée du dernier rapport de résultats",
        "template": (
            "Explique le dernier rapport de résultats financiers de {nom} ({ticker}). "
            "Si le texte intégral du rapport a été fourni ci-dessous, base ton analyse "
            "PRIORITAIREMENT dessus (c'est la source la plus fiable et la plus complète) ; "
            "sinon, appuie-toi sur les actualités récentes fournies. Décompose :\n"
            "• Chiffre d'affaires vs attentes\n"
            "• Bénéfice vs attentes\n"
            "• Indicateurs clés suivis par les investisseurs\n"
            "• Perspectives de la direction\n"
            "• Réaction du marché\n\n"
            "Si une de ces informations n'apparaît ni dans le rapport fourni ni dans les "
            "actualités, dis-le clairement plutôt que d'inventer des chiffres."
        ),
    },
}


@st.cache_data(ttl=3600, show_spinner=False)
def generer_analyse_detaillee_llm(type_analyse, ticker, nom, secteur, verdict_ia, score_ia, bullets_txt,
                                   profil_resume, news_txt=(), google_data=None,
                                   model=OPENROUTER_MODELE_GRATUIT, temperature=0.2,
                                   texte_rapport_upload=None):
    """Variante « analyse détaillée » de generer_analyse_llm : utilise un des prompts de
    recherche actions professionnelle de PROMPTS_ANALYSE_DETAILLEE (adapté automatiquement au
    ticker/nom/secteur de l'action sélectionnée), avec le même ancrage sur les données réelles
    déjà calculées pour éviter toute invention de chiffre. `texte_rapport_upload` (optionnel) :
    texte extrait d'un rapport de résultats uploadé par l'utilisateur (voir
    extraire_texte_rapport_uploade) — utilisé en priorité par le type d'analyse 'resultats'.
    Mis en cache 1h par (type, titre, modèle, rapport). Retourne le texte, None si pas de clé
    API, ou '__ERROR__:...' en cas d'échec."""
    contexte = "\n".join(f"- {b}" for b in bullets_txt)
    contexte_news = "\n".join(f"- {n}" for n in news_txt)
    contexte_google = "\n".join(f"- {libelle} : {valeur}" for libelle, valeur in (google_data or {}).items())
    consigne = PROMPTS_ANALYSE_DETAILLEE.get(type_analyse, PROMPTS_ANALYSE_DETAILLEE["wallstreet"])
    consigne_texte = consigne["template"].format(ticker=ticker, nom=nom, secteur=secteur)

    system_prompt = (
        "Tu es un analyste financier senior qui rédige en français une note de recherche "
        "actions structurée et rigoureuse, pour un investisseur particulier averti. On te "
        "fournit des signaux fondamentaux déjà calculés à partir de données réelles (Yahoo "
        "Finance), éventuellement complétés par des indicateurs Google Finance, une sélection "
        "d'actualités récentes, et parfois le texte intégral d'un rapport de résultats fourni "
        "par l'utilisateur. Structure ta réponse avec les puces/sections demandées dans la "
        "consigne. N'invente JAMAIS de chiffre, de date ou d'événement qui ne t'a pas été "
        "fourni : si une information demandée n'est pas disponible dans les données ci-dessous, "
        "dis-le explicitement au lieu de l'inventer. Termine toujours par une phrase rappelant "
        "que ceci n'est pas un conseil en investissement personnalisé."
    )
    user_message = (
        f"{consigne_texte}\n\n"
        f"--- Données réelles disponibles sur {nom} ({ticker}) ---\n"
        f"Secteur : {secteur}\n"
        f"Verdict automatique (score {score_ia:+d}) : {verdict_ia}\n\n"
        f"Signaux fondamentaux détectés (Yahoo Finance) :\n{contexte}\n\n"
        + (f"Indicateurs complémentaires (Google Finance) :\n{contexte_google}\n\n" if contexte_google else "")
        + (f"Activité de l'entreprise : {profil_resume}\n\n" if profil_resume else "")
        + (f"Actualités récentes (titre, source, date) :\n{contexte_news}\n\n" if contexte_news else "")
        + (
            f"Texte intégral du rapport de résultats fourni par l'utilisateur (source "
            f"principale, prioritaire sur les actualités ci-dessus) :\n{texte_rapport_upload}\n\n"
            if texte_rapport_upload else ""
        )
        + "Rédige ton analyse maintenant, en français."
    )

    return _appel_openrouter(system_prompt, user_message, model=model, temperature=temperature, max_tokens=2400)


def afficher_assistant_ia_chat(d, verdict_ia, score_ia, bullets_ia):
    """Assistant conversationnel contextuel à l'action affichée : le LLM reçoit ses
    fondamentaux (Yahoo Finance), les indicateurs Google Finance (best-effort), son profil
    et ses actualités récentes, et peut en plus chercher sur le web en direct (plugin
    OpenRouter, activé par défaut — voir avertissement de coût dans le toggle)."""
    ticker = d['Ticker']
    nom = d.get('Nom', ticker)

    if not _get_openrouter_api_key():
        st.info(
            "🔑 Aucune clé API OpenRouter détectée. Crée un compte gratuit sur "
            "https://openrouter.ai/keys, puis ajoute-la dans `.streamlit/secrets.toml` : "
            "`OPENROUTER_API_KEY = \"sk-or-...\"` (ou en variable d'environnement "
            "`OPENROUTER_API_KEY`). Voir le README."
        )
        return

    # Le modèle et la température sont choisis une fois via le sélecteur global affiché
    # juste au-dessus (voir afficher_verdict_ia) ; on relit simplement les valeurs mémorisées
    # ici pour rester synchro.
    modele_choisi = st.session_state.get("modele_llm_choisi", OPENROUTER_MODELE_GRATUIT)
    temperature_choisie = st.session_state.get("temperature_llm_choisie", 0.2)
    st.caption(f"🧠 Modèle actif : {MODELES_LLM_DISPONIBLES.get(modele_choisi, modele_choisi)} · Sensibilité IA : {temperature_choisie}")

    web_live = st.toggle(
        "🌐 Recherche web en direct par l'IA",
        value=True,
        key=f"chat_web_live_{ticker}",
        help="Plugin de recherche web d'OpenRouter : le modèle peut chercher sur internet en "
             "plus du contexte fourni (fondamentaux, actualités). Ce plugin n'est PAS gratuit "
             "même avec un modèle ':free' — facturé à l'usage sur ton compte OpenRouter.",
    )

    bullets_txt_c = [f"{icon} {texte}" for icon, texte in bullets_ia]
    profil_c = get_company_profile(ticker)
    google_data_c = get_google_finance_data(ticker)
    news_c = get_quick_news(ticker, nom)[:8]

    contexte_fondamentaux = "\n".join(f"- {b}" for b in bullets_txt_c) or "(aucun signal calculable)"
    contexte_google_c = "\n".join(f"- {libelle} : {valeur}" for libelle, valeur in google_data_c.items())
    contexte_news_c = "\n".join(
        f"- {n['titre']} ({n['source']}, {n['date']}, sentiment {n['sentiment']})" for n in news_c
    )

    system_prompt_chat = (
        "Tu es un assistant financier conversationnel en français, intégré à une application "
        f"d'analyse boursière. L'utilisateur consulte actuellement : {nom} ({ticker}), secteur "
        f"{d.get('Secteur', 'N/A')}, prix actuel {d.get('Prix Actuel', 'N/A')}, verdict "
        f"automatique (score {score_ia:+d}) : {verdict_ia}.\n\n"
        f"Signaux fondamentaux (Yahoo Finance) :\n{contexte_fondamentaux}\n\n"
        + (f"Indicateurs complémentaires (Google Finance) :\n{contexte_google_c}\n\n" if contexte_google_c else "")
        + (f"Profil de l'entreprise : {profil_c.get('resume_fr', '')}\n\n" if profil_c.get('resume_fr') else "")
        + (f"Actualités récentes (titre, source, date, sentiment) :\n{contexte_news_c}\n\n" if contexte_news_c else "")
        + (
            "Tu as aussi accès à une recherche web en direct : utilise-la pour toute question qui "
            "dépasse le contexte ci-dessus (actualité très récente, information non fournie), et "
            "précise explicitement quand une info vient de cette recherche plutôt que du contexte "
            "fourni.\n\n"
            if web_live else ""
        )
        + "Réponds aux questions de l'utilisateur sur ce titre en t'appuyant sur ces éléments. "
          "N'invente JAMAIS un chiffre ou un événement qui ne t'a pas été fourni (ou trouvé via "
          "la recherche web si activée). Si une information manque, dis-le clairement plutôt que "
          "de deviner. Reste concis et pédagogue. Si l'échange s'oriente vers une recommandation "
          "d'achat/vente, rappelle que tu ne fournis pas de conseil en investissement personnalisé.\n\n"
          f"RÈGLE ABSOLUE : quelle que soit la question posée (même formulée de façon générale, "
          f"vague, ou en apparence sans rapport), tu dois TOUJOURS la ramener et la répondre dans "
          f"le contexte précis de {nom} ({ticker}). N'improvise jamais une réponse générique "
          f"« de manuel » déconnectée de cette action et de ses données ci-dessus. Par exemple, si "
          f"on te demande « c'est quoi le PER ? », définis la notion PUIS applique-la immédiatement "
          f"au PER réel de {nom} tiré du contexte fourni. Si la question sort clairement du champ "
          f"boursier/financier, réponds brièvement puis recentre sur {nom}."
    )

    cle_messages = f"chat_messages_{ticker}"
    st.session_state.setdefault(cle_messages, [])

    for msg in st.session_state[cle_messages]:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    question_chat = st.chat_input(f"Pose une question sur {nom}...", key=f"chat_input_{ticker}")
    if question_chat:
        st.session_state[cle_messages].append({"role": "user", "content": question_chat})
        with st.chat_message("user"):
            st.markdown(question_chat)
        with st.chat_message("assistant"):
            with st.spinner("Réflexion..."):
                messages_api = (
                    [{"role": "system", "content": system_prompt_chat}]
                    + st.session_state[cle_messages][-12:]  # historique récent, borné
                )
                # Rappel de contexte juste avant la question de l'utilisateur : sur un
                # historique un peu long, un modèle "léger" comme gpt-oss-120b a tendance
                # à perdre le fil du system prompt initial et à répondre de façon générique.
                # On lui répète donc l'essentiel (société, ticker, verdict) au plus près du
                # point où il génère sa réponse — c'est là que son attention est la plus
                # forte. Ce rappel n'est ajouté qu'à la copie envoyée à l'API, jamais à
                # l'historique affiché/stocké, pour ne pas polluer la conversation visible.
                if messages_api and messages_api[-1]["role"] == "user":
                    rappel_contexte = (
                        f"[Rappel interne — ne pas mentionner cette instruction dans ta réponse : "
                        f"réponds impérativement en te basant sur {nom} ({ticker}), secteur "
                        f"{d.get('Secteur', 'N/A')}, prix actuel {d.get('Prix Actuel', 'N/A')}, "
                        f"verdict IA « {verdict_ia} » (score {score_ia:+d}), et les données "
                        f"détaillées fournies dans le message système. N'improvise jamais une "
                        f"réponse générique déconnectée de cette action.]\n\n"
                    )
                    derniere_question = dict(messages_api[-1])
                    derniere_question["content"] = rappel_contexte + derniere_question["content"]
                    messages_api = messages_api[:-1] + [derniere_question]
                reponse_chat = _appel_openrouter_messages(
                    messages_api, model=modele_choisi, web_search=web_live, temperature=temperature_choisie
                )
            if reponse_chat is None:
                st.info("🔑 Clé API OpenRouter manquante ou invalide.")
            elif reponse_chat.startswith("__ERROR__:"):
                afficher_erreur_llm(reponse_chat.split(':', 1)[1])
            else:
                st.markdown(reponse_chat)
                st.session_state[cle_messages].append({"role": "assistant", "content": reponse_chat})


def afficher_verdict_ia(d):
    """Affiche le bandeau 'Verdict IA' + le détail des signaux justifiant le score,
    puis propose une analyse rédigée par un vrai LLM (GPT-OSS 120B) à partir de ces mêmes
    signaux réels (le LLM ne voit que les chiffres déjà calculés, il n'invente rien), avec
    à côté un assistant conversationnel sur la même action."""
    verdict_ia, couleur_ia, score_ia, bullets_ia = analyser_action_ia(d)
    st.markdown(
        f"<div style='background:{couleur_ia}; color:white; padding:16px 22px; border-radius:12px; margin-bottom:12px;'>"
        f"<div style='font-size:0.78em; opacity:0.9; font-weight:600; letter-spacing:0.5px;'>🤖 VERDICT IA — analyse automatique sur données réelles (score {score_ia:+d})</div>"
        f"<div style='font-size:1.5em; font-weight:bold; margin-top:4px;'>{verdict_ia}</div>"
        f"</div>",
        unsafe_allow_html=True,
    )
    with st.expander("📋 Pourquoi ce verdict ? (détail des signaux)", expanded=False):
        for icon, texte in bullets_ia:
            st.markdown(f"{icon} {texte}")
        st.caption(
            "⚠️ Analyse générée automatiquement à partir des données fondamentales réelles "
            "(Yahoo Finance). Elle synthétise des règles transparentes (valorisation, santé "
            "financière, rentabilité, endettement, croissance, consensus analystes) mais ne "
            "constitue pas un conseil en investissement personnalisé. Fais tes propres recherches."
        )

    # --- Choix du modèle IA (partagé par l'analyse détaillée et l'assistant conversationnel) ---
    st.markdown("")
    modele_ia_actif, temperature_ia_active = selecteur_modele_llm(key_suffix=f"_verdict_{d['Ticker']}")

    # --- Barre des 3 actions IA : Analyse rapide | Analyse détaillée | Assistant IA ---
    col_rapide, col_detaillee, col_chat = st.columns(3)

    with col_rapide:
        lancer_llm = st.button(
            "⚡ Analyse rapide",
            key=f"llm_btn_{d['Ticker']}",
            help="Un vrai modèle de langage rédige une synthèse courte en langage naturel à "
                 "partir des signaux ci-dessus, avec le modèle sélectionné ci-dessus.",
            use_container_width=True,
        )

    with col_detaillee:
        if st.button(
            "📊 Analyse détaillée",
            key=f"detaillee_toggle_btn_{d['Ticker']}",
            help="Ouvre/ferme le choix du type de note détaillée à générer (façon analyste "
                 "actions professionnel), affiché en pleine largeur ci-dessous.",
            use_container_width=True,
        ):
            cle_ouverture_d = f"detaillee_open_{d['Ticker']}"
            st.session_state[cle_ouverture_d] = not st.session_state.get(cle_ouverture_d, False)

    with col_chat:
        if st.button(
            f"💬 Assistant IA — {d.get('Nom', d['Ticker'])}",
            key=f"chat_toggle_btn_{d['Ticker']}",
            help="Ouvre/ferme l'assistant conversationnel sur ce titre, affiché en pleine "
                 "largeur ci-dessous pour faciliter la lecture.",
            use_container_width=True,
        ):
            cle_ouverture = f"chat_open_{d['Ticker']}"
            st.session_state[cle_ouverture] = not st.session_state.get(cle_ouverture, False)

    # --- Résultat de l'analyse rapide, pleine largeur ---
    if lancer_llm:
        if not _get_openrouter_api_key():
            st.info(
                "🔑 Aucune clé API OpenRouter détectée. Crée un compte gratuit sur "
                "https://openrouter.ai/keys, puis ajoute-la dans `.streamlit/secrets.toml` : "
                "`OPENROUTER_API_KEY = \"sk-or-...\"` (ou en variable d'environnement "
                "`OPENROUTER_API_KEY`). Seule la clé de compte est requise. Voir le README."
            )
        else:
            profil = get_company_profile(d['Ticker'])
            bullets_txt = tuple(f"{icon} {texte}" for icon, texte in bullets_ia)
            news_recentes = get_quick_news(d['Ticker'], d.get('Nom', d['Ticker']))[:8]
            news_txt = tuple(
                f"{n['titre']} ({n['source']}, {n['date']}, sentiment {n['sentiment']})"
                for n in news_recentes
            )
            google_data = get_google_finance_data(d['Ticker'])
            with st.spinner(f"{MODELES_LLM_DISPONIBLES.get(modele_ia_actif, modele_ia_actif)} rédige son analyse..."):
                resultat = generer_analyse_llm(
                    ticker=d['Ticker'],
                    nom=d.get('Nom', d['Ticker']),
                    secteur=d.get('Secteur', 'N/A'),
                    verdict_ia=verdict_ia,
                    score_ia=score_ia,
                    bullets_txt=bullets_txt,
                    profil_resume=profil.get('resume_fr', ''),
                    news_txt=news_txt,
                    google_data=google_data,
                    model=modele_ia_actif,
                    temperature=temperature_ia_active,
                )
            st.divider()
            if resultat is None:
                st.info("🔑 Clé API OpenRouter manquante ou invalide.")
            elif resultat.startswith("__ERROR__:"):
                afficher_erreur_llm(resultat.split(':', 1)[1])
            else:
                st.markdown(f"#### ⚡ Analyse rapide par {MODELES_LLM_DISPONIBLES.get(modele_ia_actif, modele_ia_actif)}")
                st.markdown(resultat)
                st.caption("⚠️ Généré par un modèle de langage à partir des signaux fondamentaux (Yahoo Finance, Google Finance) et de l'actualité récente ci-dessus. Ne constitue pas un conseil en investissement personnalisé.")

    # --- Choix du type + génération de l'analyse détaillée, pleine largeur quand ouvert ---
    if st.session_state.get(f"detaillee_open_{d['Ticker']}", False):
        st.divider()
        type_analyse_choisi = st.selectbox(
            "📊 Type d'analyse détaillée",
            options=list(PROMPTS_ANALYSE_DETAILLEE.keys()),
            format_func=lambda k: PROMPTS_ANALYSE_DETAILLEE[k]["label"],
            key=f"type_analyse_detaillee_{d['Ticker']}",
            help="Choisis l'angle de la note de recherche à générer (façon analyste actions "
                 "professionnel). Le prompt est automatiquement adapté à l'action sélectionnée.",
        )

        fichier_rapport = None
        if type_analyse_choisi == "resultats":
            fichier_rapport = st.file_uploader(
                "📄 Rapport de résultats (PDF ou .txt) — optionnel mais recommandé",
                type=["pdf", "txt"],
                key=f"rapport_upload_{d['Ticker']}",
                help="Charge le rapport de résultats récupéré sur internet (communiqué de "
                     "presse, rapport trimestriel/annuel...). L'IA analysera en priorité ce "
                     "document plutôt que les actualités génériques déjà collectées.",
            )
            if fichier_rapport is None:
                st.caption(
                    "ℹ️ Sans fichier, l'analyse se basera sur les actualités récentes déjà "
                    "collectées pour ce titre (moins précis qu'un vrai rapport)."
                )

        lancer_llm_detaille = st.button(
            "📊 Générer cette analyse détaillée",
            key=f"llm_btn_detaille_{d['Ticker']}",
            help="Génère une note de recherche structurée et plus longue, selon le type "
                 "d'analyse choisi ci-dessus, avec le modèle sélectionné plus haut.",
        )
        if lancer_llm_detaille:
            if not _get_openrouter_api_key():
                st.info(
                    "🔑 Aucune clé API OpenRouter détectée. Crée un compte gratuit sur "
                    "https://openrouter.ai/keys, puis ajoute-la dans `.streamlit/secrets.toml` : "
                    "`OPENROUTER_API_KEY = \"sk-or-...\"` (ou en variable d'environnement "
                    "`OPENROUTER_API_KEY`). Seule la clé de compte est requise. Voir le README."
                )
            else:
                texte_rapport, erreur_rapport = extraire_texte_rapport_uploade(fichier_rapport)
                if erreur_rapport:
                    st.warning(f"⚠️ {erreur_rapport} L'analyse se poursuit sans le rapport.")
                profil = get_company_profile(d['Ticker'])
                bullets_txt = tuple(f"{icon} {texte}" for icon, texte in bullets_ia)
                news_recentes = get_quick_news(d['Ticker'], d.get('Nom', d['Ticker']))[:8]
                news_txt = tuple(
                    f"{n['titre']} ({n['source']}, {n['date']}, sentiment {n['sentiment']})"
                    for n in news_recentes
                )
                google_data = get_google_finance_data(d['Ticker'])
                with st.spinner(f"{MODELES_LLM_DISPONIBLES.get(modele_ia_actif, modele_ia_actif)} rédige la note détaillée..."):
                    resultat_d = generer_analyse_detaillee_llm(
                        type_analyse=type_analyse_choisi,
                        ticker=d['Ticker'],
                        nom=d.get('Nom', d['Ticker']),
                        secteur=d.get('Secteur', 'N/A'),
                        verdict_ia=verdict_ia,
                        score_ia=score_ia,
                        bullets_txt=bullets_txt,
                        profil_resume=profil.get('resume_fr', ''),
                        news_txt=news_txt,
                        google_data=google_data,
                        model=modele_ia_actif,
                        temperature=temperature_ia_active,
                        texte_rapport_upload=texte_rapport,
                    )
                if resultat_d is None:
                    st.info("🔑 Clé API OpenRouter manquante ou invalide.")
                elif resultat_d.startswith("__ERROR__:"):
                    afficher_erreur_llm(resultat_d.split(':', 1)[1])
                else:
                    st.markdown(
                        f"#### 📊 {PROMPTS_ANALYSE_DETAILLEE[type_analyse_choisi]['label']} — "
                        f"par {MODELES_LLM_DISPONIBLES.get(modele_ia_actif, modele_ia_actif)}"
                    )
                    st.markdown(resultat_d)
                    st.caption("⚠️ Généré par un modèle de langage à partir des signaux fondamentaux (Yahoo Finance, Google Finance) et de l'actualité récente ci-dessus. Ne constitue pas un conseil en investissement personnalisé.")

    # --- Assistant conversationnel, affiché en pleine largeur quand ouvert ---
    if st.session_state.get(f"chat_open_{d['Ticker']}", False):
        st.divider()
        st.markdown(f"#### 💬 Assistant IA — {d.get('Nom', d['Ticker'])}")
        afficher_assistant_ia_chat(d, verdict_ia, score_ia, bullets_ia)


def afficher_profil_societe(ticker):
    """Affiche un résumé qualitatif de la société (description, secteur, pays, effectifs)."""
    profil = get_company_profile(ticker)
    texte = profil.get('resume_fr') or profil.get('resume')
    if not texte and profil.get('secteur', 'N/A') == 'N/A':
        return
    with st.expander("🏢 Profil de la société"):
        st.write(f"**Secteur :** {profil.get('secteur', 'N/A')}  |  **Industrie :** {profil.get('industrie', 'N/A')}")
        ligne_pays = f"**Pays :** {profil.get('pays', 'N/A')}"
        if profil.get('employes'):
            ligne_pays += f"  |  **Employés :** {profil['employes']:,}".replace(',', ' ')
        st.write(ligne_pays)
        if profil.get('site'):
            st.write(f"**Site web :** {profil['site']}")
        if texte:
            st.write(texte)


def afficher_detail_action(d):
    """Fiche détaillée d'une action (santé financière, graphique, valorisation, actualités).
    Réutilisée depuis la vue Portefeuille et depuis la vue Indices."""
    fd = d['full_data']
    st.divider()

    c1, c2 = st.columns([2, 1])

    with c1:
        st.header(f"🏢 {d['Nom']} ({d['Ticker']})")

        afficher_verdict_ia(d)

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
            if not h_data_large.empty:
                # Le Close du dernier jour peut arriver en retard chez Yahoo alors que le
                # Volume est déjà disponible : on le complète avec le prix actuel plutôt
                # que de supprimer la ligne (ce qui effacerait aussi le volume du jour).
                if pd.isna(h_data_large['Close'].iloc[-1]) and d.get('Prix Actuel'):
                    h_data_large.loc[h_data_large.index[-1], 'Close'] = d['Prix Actuel']
                # On ne retire que les lignes réellement vides (ni Close ni Volume) :
                # weekends / jours fériés remontés par erreur.
                h_data_large = h_data_large[h_data_large['Close'].notna() | h_data_large['Volume'].notna()]

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
                    name="Prix", line=dict(color='#1a73e8', width=2),
                    connectgaps=True
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
                if fd.get('val_bna', 0) and fd['val_bna'] > 0:
                    fig.add_shape(
                        type="line", xref="paper", x0=0, x1=1,
                        yref="y2", y0=fd['val_bna'] * 0.85, y1=fd['val_bna'] * 0.85,
                        line=dict(color="#28a745", dash="dot", width=1.5)
                    )
                    fig.add_annotation(
                        xref="paper", x=0, yref="y2",
                        y=fd['val_bna'] * 0.85, text="Entrée BNA -15%  ",
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
                    width='stretch',
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
            ("1️⃣ Modèle BNA (Forward)", fd['val_bna'],
             (f"BNA Pondéré 30% Fwd/70% Actuel ({clean_num(fd['eps_fwd_valo'])}"
              + (" plafonné à +50%" if fd['eps_fwd_valo'] != fd['eps_fwd'] else "")
              + f") × {fd['per_source']} ({clean_num(fd['per_valo_bna'])})")),
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

        afficher_profil_societe(d['Ticker'])

        ticker_clean   = str(d['Ticker']).split('.')[0].upper() if d and 'Ticker' in d else "AAPL"
        nom_action_vue = d.get('Nom', ticker_clean)

        st.divider()
        col_titre, col_switch = st.columns([3, 1])
        with col_titre:
            st.markdown(f"### 📰 Dernières Actualités : {nom_action_vue}")
        with col_switch:
            mode_fr = st.toggle("FR", help="Traduction automatique des titres en français", value=False)

        all_news = get_quick_news(d['Ticker'], nom_action_vue)

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


ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")


@st.cache_data(ttl=86400)
def resolve_isin_to_yahoo_ticker(isin):
    """
    Convertit un code ISIN en ticker Yahoo Finance via l'endpoint de recherche
    utilisé par le site Yahoo Finance lui-même (non officiel, mais pratique car
    de nombreux fonds/ETF européens sont plus facilement identifiables par leur
    ISIN que par leur ticker de cotation, qui varie selon la place boursière).
    Retourne le premier ticker trouvé, ou None si rien n'est trouvé.
    NB : ceci ne fait que retrouver le BON ticker à interroger — si le fonds en
    question ne publie pas sa composition sur Yahoo Finance (cas fréquent pour
    les fonds/ETF non listés chez les grands émetteurs), get_etf_top_holdings
    restera vide même avec le ticker correct.
    """
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        resp = requests.get(
            "https://query2.finance.yahoo.com/v1/finance/search",
            params={"q": isin, "quotesCount": 5, "newsCount": 0},
            headers=headers, timeout=10
        )
        resp.raise_for_status()
        quotes = resp.json().get("quotes", [])
        if quotes:
            return quotes[0].get("symbol")
    except Exception:
        pass
    return None


@st.cache_data(ttl=86400)
def get_etf_top_holdings(etf_ticker):
    """
    Récupère les principales lignes (généralement 10 à 25 selon l'émetteur) qui
    composent un ETF, via l'attribut `funds_data` de yfinance (nécessite
    yfinance >= 0.2.38).
    Retourne un tuple (composants, poids) où :
      - composants est une liste de tuples (ticker_yf, nom), au même format que
        get_index_constituents, réutilisable directement avec get_index_market_data.
      - poids est un dict {ticker_yf: poids_en_pourcentage}.
    NB : c'est une composition PARTIELLE (top holdings), pas la composition
    complète de l'ETF — Yahoo ne fournit pas toujours cette donnée, en
    particulier pour les ETF UCITS domiciliés en Europe (couverture inégale
    selon l'émetteur). Si `top_holdings` est vide, il faut passer par le
    fichier de composition officiel de l'émetteur (iShares/Amundi/Vanguard...).
    """
    try:
        etf = yf.Ticker(etf_ticker)
        holdings_df = etf.funds_data.top_holdings
    except Exception as e:
        st.error(f"Impossible de récupérer la composition de {etf_ticker} : {e}")
        return [], {}

    if holdings_df is None or holdings_df.empty:
        st.warning(
            f"Aucune composition disponible pour {etf_ticker} via Yahoo Finance "
            f"(fréquent sur les ETF UCITS européens). Une intégration du fichier "
            f"de composition officiel de l'émetteur serait nécessaire."
        )
        return [], {}

    composants = []
    poids = {}
    for tk_brut, row in holdings_df.iterrows():
        # Contrairement aux indices scrapés sur Wikipedia, les tickers renvoyés ici par
        # yfinance sont déjà au format Yahoo complet, suffixe de place boursière inclus
        # (ex: "ASML.AS", "HSBA.L", "SIE.DE") — on ne touche pas aux points.
        tk = str(tk_brut).strip().upper()
        nom = row.get("Name", tk)
        pct = row.get("Holding Percent", None)
        composants.append((tk, nom))
        if pct is not None and not pd.isna(pct):
            # yfinance renvoie une fraction (0.072) plutôt qu'un pourcentage (7.2)
            poids[tk] = pct * 100 if pct <= 1 else pct

    return composants, poids


_YAHOO_QUOTE_HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}


def _yahoo_quote_batch(tickers, chunk_size=150, max_workers=6):
    """
    Interroge l'endpoint 'quote' officiel de Yahoo Finance PAR LOT (jusqu'à
    `chunk_size` tickers dans une seule requête HTTP), au lieu d'un appel
    séparé par titre. C'est ce qui fait toute la différence de vitesse sur un
    gros indice (ex: S&P 500) : on passe de ~500 requêtes réseau à quelques
    unes seulement, chacune renvoyant prix, variation du jour, volume,
    capitalisation, PER et P/B pour tout le lot en une fois.
    Retourne un dict {ticker: {champs...}}. Si l'appel échoue pour un lot
    (Yahoo peut occasionnellement bloquer/rate-limiter cet endpoint), ce lot
    est simplement absent du résultat — l'appelant doit prévoir un repli.
    """
    tickers = list(dict.fromkeys(tickers))  # dédoublonne en gardant l'ordre
    resultat = {}

    url = "https://query1.finance.yahoo.com/v7/finance/quote"

    def _one_chunk(chunk):
        params = {"symbols": ",".join(chunk)}
        try:
            if _YF_SESSION is not None:
                # Session yfinance : crumb/cookie déjà gérés, requête authentifiée.
                r = _YF_SESSION.get(url, params=params)
            else:
                r = requests.get(url, params=params, headers=_YAHOO_QUOTE_HEADERS, timeout=10)
            r.raise_for_status()
            return r.json().get("quoteResponse", {}).get("result", [])
        except Exception:
            return []

    chunks = [tickers[i:i + chunk_size] for i in range(0, len(tickers), chunk_size)]

    if _YF_SESSION is not None and chunks:
        # Appel "à vide" (un seul symbole) hors du pool de threads : force
        # yfinance à résoudre son crumb/cookie une bonne fois pour toutes
        # avant que plusieurs threads ne tentent de le faire en même temps
        # (source classique de 401 intermittents sur le tout premier lot).
        try:
            _YF_SESSION.get(url, params={"symbols": chunks[0][0]})
        except Exception:
            pass

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for quotes in executor.map(_one_chunk, chunks):
            for q in quotes:
                sym = q.get("symbol")
                if sym:
                    resultat[sym] = q
    return resultat


@st.cache_data(ttl=300)
def get_index_market_data(tickers_noms, max_workers=8, _nonce=0):
    """
    Prix, variation du jour, variation YTD, volume et montant échangé.

    Version optimisée : au lieu d'interroger Yahoo Finance UN TITRE À LA FOIS
    (ce qui, sur un indice de 500 valeurs, déclenchait des centaines de
    requêtes HTTP séquentielles/threadées avec retries et pauses), on fait :
      1) UN seul appel groupé (par lots) à l'API "quote" de Yahoo pour le prix,
         la variation du jour et le volume temps réel de TOUS les titres ;
      2) UN seul téléchargement groupé (yf.download) pour l'historique YTD,
         nécessaire au calcul de la variation YTD et des moyennes de
         volume/montant.
    Résultat : quelques requêtes réseau au total au lieu d'une par titre.
    Le paramètre _nonce ne sert qu'à invalider le cache Streamlit sur demande
    (bouton "Réessayer").
    """
    noms_map = dict(tickers_noms)
    tickers = [t for t, n in tickers_noms]
    if not tickers:
        return pd.DataFrame(), []

    erreurs = []

    # --- 1) Cours / variation jour / volume temps réel, en un seul lot ---
    quotes = _yahoo_quote_batch(tickers)

    # --- 2) Historique YTD groupé (pour VarYTD + moyennes volume/montant) ---
    hist_all = None
    try:
        hist_all = yf.download(
            tickers, period="ytd", interval="1d", group_by="ticker",
            threads=True, progress=False, auto_adjust=False,
        )
    except Exception as e:
        erreurs.append(f"Téléchargement groupé de l'historique : {e}")

    def _hist_pour(tk):
        if hist_all is None or hist_all.empty:
            return None
        try:
            h = hist_all[tk] if len(tickers) > 1 else hist_all
            h = h.dropna(subset=["Close"])
            return h if not h.empty else None
        except Exception:
            return None

    lignes = []
    for tk in tickers:
        q = quotes.get(tk, {})
        h = _hist_pour(tk)

        prix     = q.get("regularMarketPrice")
        var_jour = q.get("regularMarketChangePercent")
        volume   = q.get("regularMarketVolume")

        var_ytd = None
        volume_moyen = None
        montant_moyen = None
        if h is not None:
            try:
                if prix is None:
                    prix = float(h["Close"].iloc[-1])
                if volume is None and pd.notna(h["Volume"].iloc[-1]):
                    volume = float(h["Volume"].iloc[-1])
                if var_jour is None and len(h) >= 2:
                    prix_veille = float(h["Close"].iloc[-2])
                    var_jour = (prix - prix_veille) / prix_veille * 100
                prix_debut_annee = float(h["Close"].iloc[0])
                var_ytd = (prix - prix_debut_annee) / prix_debut_annee * 100
                vol_serie = h["Volume"].dropna()
                if not vol_serie.empty:
                    volume_moyen = float(vol_serie.mean())
                    montant_moyen = float((h["Close"] * h["Volume"]).dropna().mean())
            except Exception:
                pass

        # Repli individuel (rare) : ni l'appel groupé ni l'historique n'ont
        # fonctionné pour ce titre précis — on le retente seul, comme avant.
        if prix is None or var_jour is None:
            try:
                t = yf.Ticker(tk)
                hist = t.history(period="ytd", interval="1d", auto_adjust=False).dropna(subset=["Close"])
                if len(hist) >= 2:
                    prix = float(hist["Close"].iloc[-1])
                    prix_veille = float(hist["Close"].iloc[-2])
                    var_jour = (prix - prix_veille) / prix_veille * 100
                    prix_debut_annee = float(hist["Close"].iloc[0])
                    var_ytd = (prix - prix_debut_annee) / prix_debut_annee * 100
                    volume = float(hist["Volume"].iloc[-1]) if pd.notna(hist["Volume"].iloc[-1]) else 0.0
                    vol_serie = hist["Volume"].dropna()
                    volume_moyen = float(vol_serie.mean()) if not vol_serie.empty else 0.0
                    montant_moyen = float((hist["Close"] * hist["Volume"]).dropna().mean()) if not vol_serie.empty else 0.0
            except Exception as e:
                erreurs.append(f"{tk} : {e}")
                continue

        if prix is None or var_jour is None:
            erreurs.append(f"{tk} : données insuffisantes")
            continue

        volume = volume or 0.0
        lignes.append({
            "Ticker": tk,
            "Nom": noms_map.get(tk, tk),
            "Prix": prix,
            "VarJourNum": var_jour,
            "VarYTDNum": var_ytd if var_ytd is not None else 0.0,
            "Volume": volume,
            "VolumeMoyen": volume_moyen if volume_moyen is not None else volume,
            "Montant": volume * prix,
            "MontantMoyen": montant_moyen if montant_moyen is not None else (volume * prix),
        })

    return pd.DataFrame(lignes), erreurs[:5]


@st.cache_data(ttl=3600)
def get_index_fondamentaux(tickers, max_workers=20):
    """
    Capitalisation, PER, PB — récupérés via l'appel groupé Yahoo (`_yahoo_quote_batch`)
    au lieu d'un `yf.Ticker(tk).info` par titre. C'est le principal gain de vitesse :
    `.info` déclenche plusieurs requêtes internes par titre chez yfinance et peut
    prendre 1 à 2 secondes PAR TITRE, soit plusieurs minutes sur un gros indice.
    Repli individuel (`.info`) uniquement pour les titres absents du lot Yahoo.
    """
    quotes = _yahoo_quote_batch(tickers)

    manquants = [tk for tk in tickers if tk not in quotes]
    infos_repli = {}
    if manquants:
        def _one(tk):
            try:
                info = yf.Ticker(tk).info
                return tk, {
                    "MarketCap": info.get("marketCap"),
                    "PE": info.get("trailingPE"),
                    "PB": info.get("priceToBook"),
                }
            except Exception:
                return tk, {"MarketCap": None, "PE": None, "PB": None}

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            for tk, vals in executor.map(_one, manquants):
                infos_repli[tk] = vals

    lignes = []
    for tk in tickers:
        q = quotes.get(tk)
        if q is not None:
            lignes.append({
                "Ticker": tk,
                "MarketCap": q.get("marketCap"),
                "PE": q.get("trailingPE"),
                "PB": q.get("priceToBook"),
            })
        else:
            vals = infos_repli.get(tk, {"MarketCap": None, "PE": None, "PB": None})
            lignes.append({"Ticker": tk, **vals})
    return pd.DataFrame(lignes)


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
            prev_close = getattr(idx.fast_info, "previous_close", None)
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
            width='stretch',
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
                st.session_state["vue_etf"] = None
                st.rerun()
    if st.session_state.get("vue_indice"):
        if st.sidebar.button("↩️ Retour", width="stretch", key="retour_indice"):
            st.session_state["vue_indice"] = None
            st.rerun()

    st.sidebar.divider()
    st.sidebar.header("🧺 ETF")
    etf_saisi = st.sidebar.text_input(
        "Ticker Yahoo Finance ou ISIN de l'ETF",
        placeholder="ex : EQQQ.PA, IWDA.AS, CSPX.L ou LU1681043599",
        key="etf_ticker_input",
        help="Ticker tel qu'utilisé par Yahoo Finance, ou code ISIN (converti "
             "automatiquement en ticker Yahoo). La composition renvoyée (top "
             "holdings) dépend de ce que Yahoo publie pour ce fonds — certains "
             "fonds/ETF ne publient aucune composition sur Yahoo, quel que soit "
             "le mode de recherche."
    )
    if st.sidebar.button("Analyser l'ETF", width="stretch", key="etf_btn_analyser") and etf_saisi.strip():
        saisie = etf_saisi.strip().upper()
        if ISIN_RE.match(saisie):
            with st.spinner(f"Résolution de l'ISIN {saisie}..."):
                ticker_resolu = resolve_isin_to_yahoo_ticker(saisie)
            if not ticker_resolu:
                st.sidebar.error(f"Aucun ticker Yahoo Finance trouvé pour l'ISIN {saisie}.")
            else:
                st.sidebar.success(f"ISIN {saisie} → ticker {ticker_resolu}")
                st.session_state["vue_etf"] = ticker_resolu
                st.session_state["vue_indice"] = None
                st.rerun()
        else:
            st.session_state["vue_etf"] = saisie
            st.session_state["vue_indice"] = None
            st.rerun()
    if st.session_state.get("vue_etf"):
        if st.sidebar.button("↩️ Retour", width="stretch", key="retour_etf"):
            st.session_state["vue_etf"] = None
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

    nb_affiche = len(df_affiche)
    st.caption(
        f"{nb_affiche} valeur{'s' if nb_affiche != 1 else ''}"
        + (f" sur {nb_composants}" if recherche and nb_affiche != nb_composants else "")
    )

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


def afficher_dashboard_etf(etf_ticker):
    """Vue principale quand un ETF est saisi : composition (top holdings) + métriques marché."""
    st.subheader(f"🧺 Composition de {etf_ticker}")

    with st.spinner(f"Récupération de la composition de {etf_ticker}..."):
        composants, poids = get_etf_top_holdings(etf_ticker)
    if not composants:
        return

    nb_composants = len(composants)
    st.caption(f"{nb_composants} lignes (top holdings communiqués par Yahoo Finance — "
               f"pas nécessairement la composition intégrale de l'ETF)")

    retry_key = f"retry_nonce_etf_{etf_ticker}"
    st.session_state.setdefault(retry_key, 0)

    with st.spinner("Récupération des cours..."):
        df, erreurs_marche = get_index_market_data(tuple(composants), _nonce=st.session_state[retry_key])
    if df.empty:
        st.warning("Données de marché indisponibles pour le moment.")
        if erreurs_marche:
            with st.expander("Détails techniques (aide au diagnostic)"):
                for e in erreurs_marche:
                    st.caption(e)
        if st.button("🔄 Réessayer", key=f"retry_etf_{etf_ticker}"):
            st.session_state[retry_key] += 1
            st.rerun()
        return

    df["PoidsNum"] = df["Ticker"].map(poids)

    charger_fondamentaux = st.checkbox(
        "Charger PER / PB (plus lent)",
        value=(nb_composants <= 50),
        key=f"fond_etf_{etf_ticker}"
    )
    if charger_fondamentaux:
        with st.spinner("Récupération des fondamentaux..."):
            df_fond = get_index_fondamentaux(df["Ticker"].tolist())
        df = df.merge(df_fond, on="Ticker", how="left")

    # Tri par poids dans l'ETF (à défaut, par variation du jour)
    if df["PoidsNum"].notna().any():
        df = df.sort_values("PoidsNum", ascending=False).reset_index(drop=True)
    else:
        df = df.sort_values("VarJourNum", ascending=False).reset_index(drop=True)

    recherche = st.text_input("🔍 Filtrer par ticker ou nom", key=f"filtre_etf_{etf_ticker}")
    if recherche:
        q = recherche.lower()
        df = df[df["Ticker"].str.lower().str.contains(q) | df["Nom"].str.lower().str.contains(q)].reset_index(drop=True)

    df_affiche = pd.DataFrame({
        "Ticker": df["Ticker"],
        "Nom": df["Nom"],
        "Poids %": df["PoidsNum"].apply(lambda v: f"{v:.2f}%" if pd.notna(v) else "N/A"),
        "Prix": df["Prix"],
        "Var Jour %": df["VarJourNum"].apply(lambda v: f"{v:+.2f}%"),
        "Volume": df["Volume"],
        "Var YTD %": df["VarYTDNum"].apply(lambda v: f"{v:+.2f}%"),
    })
    if charger_fondamentaux:
        df_affiche["MarketCap"] = df["MarketCap"]
        df_affiche["PE"] = df["PE"]
        df_affiche["PB"] = df["PB"]

    hauteur = min((len(df_affiche) * 35) + 38, 850)

    nb_affiche = len(df_affiche)
    st.caption(
        f"{nb_affiche} ligne{'s' if nb_affiche != 1 else ''}"
        + (f" sur {nb_composants}" if recherche and nb_affiche != nb_composants else "")
    )

    sel = st.dataframe(
        df_affiche.style.format(formatter=lambda x: clean_num(x) if isinstance(x, (int, float)) else x),
        on_select="rerun",
        selection_mode="single-row",
        width="stretch",
        hide_index=True,
        height=hauteur,
    )

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
TDM_TICKERS_SBF120 = TDM_TICKERS_CAC40 + [
    # CAC Next 20 + principales valeurs moyennes du CAC Mid 60 (échantillon indicatif :
    # le SBF 120 est révisé trimestriellement par Euronext, composition complète non
    # praticable en dur — même remarque que pour le Russell 2000 / MSCI World plus bas).
    "AMUN.PA", "RXL.PA", "GET.PA", "FGR.PA", "GFC.PA", "IPN.PA", "SOP.PA", "BVI.PA",
    "ELIS.PA", "LI.PA", "NEX.PA", "RCO.PA", "DEC.PA", "COFA.PA", "UBI.PA", "FR.PA",
    "ATE.PA", "NEOEN.PA", "ILD.PA", "AF.PA", "VK.PA", "TE.PA", "ENX.PA", "VRLA.PA",
    "MF.PA", "FDJ.PA", "SCR.PA", "RBT.PA", "ITP.PA", "TRI.PA", "VCT.PA", "BB.PA",
    "CGG.PA", "DBG.PA", "GTT.PA", "NXI.PA", "RUI.PA", "SPIE.PA", "SW.PA", "MRN.PA",
    "POM.PA", "TFI.PA", "SEV.PA", "LTA.PA",
]

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

TDM_TICKERS_KOSPI = [
    "005930.KS", "000660.KS", "373220.KS", "207940.KS", "005380.KS", "000270.KS",
    "068270.KS", "035420.KS", "005490.KS", "006400.KS", "051910.KS", "105560.KS",
    "055550.KS", "086790.KS", "012330.KS", "096770.KS", "066570.KS", "028260.KS",
    "017670.KS", "015760.KS", "032830.KS", "009830.KS", "010950.KS", "035720.KS",
    "003550.KS", "329180.KS", "034020.KS", "316140.KS", "030200.KS", "090430.KS"
]

TDM_INDEX_TICKER_MAP = {
    "NASDAQ 100 Tech": TDM_TICKERS_NASDAQ,
    "CAC 40 (France)": TDM_TICKERS_CAC40,
    "SBF 120 (France)": TDM_TICKERS_SBF120,
    "DAX (Allemagne)": TDM_TICKERS_DAX,
    "S&P 500 Lite (USA)": TDM_TICKERS_SP500,
    "S&P 500 Full (USA)": TDM_TICKERS_SP500,
    "Nikkei 225 (Japon)": TDM_TICKERS_NIKKEI,
    "EURO STOXX 50": TDM_TICKERS_EUROSTOXX50,
    "FTSE 100 (Royaume-Uni)": TDM_TICKERS_FTSE100,
    "Russell 2000 (États-Unis)": TDM_TICKERS_RUSSELL2000,
    "MSCI World": TDM_TICKERS_MSCI_WORLD,
    "MSCI Emerging Markets": TDM_TICKERS_MSCI_EM,
    "FTSE MIB (Italie)": TDM_TICKERS_FTSEMIB,
    "NIFTY 50 (Inde)": TDM_TICKERS_NIFTY50,
    "FTSE China 50": TDM_TICKERS_FTSECHINA50,
    "KOSPI (Corée du Sud)": TDM_TICKERS_KOSPI,
}

# Indices pour lesquels on dispose d'un scraper Wikipedia (voir INDICES_PRINCIPAUX /
# get_index_constituents plus haut) donnant la composition RÉELLE et à jour, au lieu
# d'une liste "indicative" codée en dur. On les fait correspondre aux clés utilisées
# dans cette page (nom différent du libellé utilisé sur la page Portefeuille).
TDM_WIKI_INDEX_NAMES = {
    "NASDAQ 100 Tech": "Nasdaq 100",
    "CAC 40 (France)": "CAC 40",
    "DAX (Allemagne)": "DAX",
    # "S&P 500 Full (USA)" utilise la composition réelle (~500 valeurs) via Wikipedia —
    # nettement plus lent (requêtes séquentielles par valeur), d'où l'entrée séparée
    # "S&P 500 Lite (USA)" qui reste sur un échantillon statique rapide.
    "S&P 500 Full (USA)": "S&P 500",
}


def tdm_resolve_index_tickers(nom_indice_tdm):
    """Retourne la liste des tickers composant l'indice sélectionné.
    Priorité à la composition réelle scrapée sur Wikipedia (même source que la page
    Portefeuille > Indices Principaux, mise en cache 24h) quand elle est disponible ;
    repli sur la liste statique "indicative" sinon (ou en cas d'échec du scraping)."""
    wiki_name = TDM_WIKI_INDEX_NAMES.get(nom_indice_tdm)
    if wiki_name:
        try:
            constituents = get_index_constituents(wiki_name)
            tickers = [tk for tk, _nom in constituents] if constituents else []
            if tickers:
                return tickers
        except Exception:
            pass
    return TDM_INDEX_TICKER_MAP.get(nom_indice_tdm, [])

# Symbole de l'indice lui-même (pour le graphique intraday agrégé)
# NOTE : pour MSCI World / MSCI Emerging Markets / FTSE China 50, Yahoo Finance ne propose
# pas de ticker d'indice directement exploitable en intraday : on utilise l'ETF de référence
# le plus liquide qui réplique l'indice (URTH, EEM, FXI) comme proxy du niveau/variation.
TDM_SYMBOL_MAP = {
    "NASDAQ 100 Tech": "^NDX",
    "CAC 40 (France)": "^FCHI",
    "SBF 120 (France)": "^SBF120",
    "DAX (Allemagne)": "^GDAXI",
    "S&P 500 Lite (USA)": "^GSPC",
    "S&P 500 Full (USA)": "^GSPC",
    "Nikkei 225 (Japon)": "^N225",
    "EURO STOXX 50": "^STOXX50E",
    "FTSE 100 (Royaume-Uni)": "^FTSE",
    "Russell 2000 (États-Unis)": "^RUT",
    "MSCI World": "URTH",
    "MSCI Emerging Markets": "EEM",
    "FTSE MIB (Italie)": "FTSEMIB.MI",
    "NIFTY 50 (Inde)": "^NSEI",
    "FTSE China 50": "FXI",
    "KOSPI (Corée du Sud)": "^KS11",
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


TDM_COMPARISON_COLORS = [
    "#2b6cb0", "#e07b39", "#38a169", "#d53f8c", "#805ad5",
    "#dd6b20", "#319795", "#e53e3e", "#718096", "#b7791f"
]


def _tdm_build_comparison_chart(data_dict, chart_title, x_title):
    """Superpose la performance (%) de plusieurs indices, rebasée à 0% au
    premier point de la période, sur un même graphique."""
    fig = go.Figure()

    for i, (nom, df) in enumerate(data_dict.items()):
        if df is None or df.empty:
            continue
        base = float(df['Close'].iloc[0])
        if not base:
            continue
        pct = ((df['Close'] - base) / base) * 100
        color = TDM_COMPARISON_COLORS[i % len(TDM_COMPARISON_COLORS)]
        fig.add_trace(go.Scatter(
            x=df.index,
            y=pct,
            mode='lines',
            name=nom,
            line=dict(color=color, width=2),
            hovertemplate=f"{nom}: " + "%{y:+.2f}%<extra></extra>"
        ))

    fig.add_hline(y=0, line_dash="dot", line_color="#999999", opacity=0.6)

    fig.update_layout(
        title=chart_title,
        height=600,
        hovermode='x unified',
        legend=dict(orientation='h', yanchor='bottom', y=1.02, xanchor='right', x=1),
        margin=dict(t=60, b=40),
        xaxis=dict(title=x_title),
        yaxis=dict(title="Performance (%)", ticksuffix="%")
    )
    return fig


@st.cache_data(ttl=60)
def tdm_get_index_daily_changes():
    """Variation du jour (dernier cours live vs clôture veille) pour chaque indice,
    utilisée pour l'affichage rapide dans le menu latéral.

    NOTE : on utilise ici la cotation live (fast_info), comme le fait le
    reste du dashboard (cf. afficher_graphique_indice), et non un
    téléchargement en masse de bougies journalières (yf.download period="5d").
    Cette dernière approche comparait deux clôtures journalières dont la
    bougie du jour n'est pas toujours actualisée aussi vite que la cotation
    live, ce qui provoquait un décalage (voire un jour de retard complet)
    entre le % affiché dans la sidebar et le % affiché en haut de page pour
    le même indice."""
    changes = {}
    symbols = list(dict.fromkeys(TDM_SYMBOL_MAP.values()))

    def _one(sym):
        try:
            fi = yf.Ticker(sym).fast_info
            last = getattr(fi, "last_price", None)
            prev = getattr(fi, "previous_close", None)
            if last and prev:
                return sym, ((float(last) - float(prev)) / float(prev)) * 100
        except Exception:
            pass
        return sym, None

    with ThreadPoolExecutor(max_workers=min(10, len(symbols) or 1)) as executor:
        results = dict(executor.map(_one, symbols))

    for name, sym in TDM_SYMBOL_MAP.items():
        val = results.get(sym)
        if val is not None:
            changes[name] = val

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
@st.cache_data(ttl=300)  # Rafraîchi toutes les 5 min (les taux de change bougent peu à cette échelle)
def tdm_get_fx_rates(currencies):
    """Retourne {code_devise: taux vers 1 EUR} pour convertir un montant dans sa devise
    de cotation d'origine (ex. INR, JPY, USD...) vers l'euro. L'EUR vaut toujours 1.0.
    En cas d'échec sur une devise (paire indisponible sur Yahoo Finance), on retombe
    sur un taux de 1.0 pour cette devise plutôt que de faire planter tout le tableau."""
    rates = {"EUR": 1.0}
    to_fetch = sorted(c for c in set(currencies) if c and c != "EUR")
    if not to_fetch:
        return rates

    fx_tickers = [f"{c}EUR=X" for c in to_fetch]
    try:
        data = yf.download(fx_tickers, period="5d", progress=False, auto_adjust=False)
        for c, tk in zip(to_fetch, fx_tickers):
            try:
                if isinstance(data.columns, pd.MultiIndex):
                    serie = data['Close', tk].dropna()
                else:
                    serie = data['Close'].dropna()
                rates[c] = float(serie.iloc[-1]) if not serie.empty else 1.0
            except Exception:
                rates[c] = 1.0
    except Exception:
        for c in to_fetch:
            rates[c] = 1.0
    return rates


def _tdm_calc_entree_conseillee(ticker_symbol, info):
    """
    Entrée conseillée (-15%) : moyenne des modèles BNA Forward/Actuel, FCF et
    Cible Analystes, puis -15%. Isolée dans sa propre fonction pour pouvoir
    être appelée en parallèle (ThreadPoolExecutor) sur tous les titres au lieu
    d'une boucle séquentielle — c'est la seule partie de cette page qui a
    encore besoin d'un accès individuel par titre (cash-flow non disponible
    en appel groupé chez Yahoo).
    """
    try:
        ticker_obj = yf.Ticker(ticker_symbol)
        # NB : `info` peut venir soit du dict `.info` de yfinance (clés "forwardEps" /
        # "trailingEps"), soit de l'appel groupé brut à l'API Yahoo `_yahoo_quote_batch`
        # (clés "epsForward" / "epsTrailingTwelveMonths"). Sans ce repli sur les deux
        # noms de clés, le BNA restait toujours à 0 pour les titres issus du lot Yahoo
        # (le cas de tout le tableau des indices), ce qui vidait "Entrée BNA -15%".
        ef = info.get('forwardEps') or info.get('epsForward') or 0
        pf = info.get('forwardPE') or 15
        trailing_eps_row = info.get('trailingEps') or info.get('epsTrailingTwelveMonths') or 0
        trailing_pe_row  = info.get('trailingPE')

        if trailing_pe_row and 5 <= trailing_pe_row <= 60:
            pf_valo = trailing_pe_row
        else:
            per_hist_moy_row = calc_per_historique_moyen(ticker_obj, years=5)
            if per_hist_moy_row and per_hist_moy_row > 0:
                pf_valo = per_hist_moy_row
            elif pf and 3 < pf <= 80:
                pf_valo = pf
            else:
                pf_valo = 15

        if trailing_eps_row and trailing_eps_row > 0 and ef:
            ef_pondere = (ef * 0.30) + (trailing_eps_row * 0.70)
        else:
            ef_pondere = ef

        ef_valo = ef_pondere
        if trailing_eps_row and trailing_eps_row > 0 and ef_pondere and ef_pondere > trailing_eps_row * 1.5:
            ef_valo = trailing_eps_row * 1.5

        vb = ef_valo * pf_valo if ef_valo else 0
        entree_bna = vb * 0.85 if vb > 0 else np.nan

        tm = info.get('targetMeanPrice') or 0

        sh_out = info.get('sharesOutstanding') or 0
        try:
            fcf_series = ticker_obj.cashflow.loc['Free Cash Flow'].dropna().head(3) \
                if 'Free Cash Flow' in ticker_obj.cashflow.index else pd.Series(dtype=float)
            fcf_raw = fcf_series.mean() if not fcf_series.empty else 0
        except Exception:
            fcf_raw = 0
        vf = (fcf_raw / sh_out * 1.05) * pf if sh_out and fcf_raw else 0

        mods = [v for v in [vb, vf, tm] if v and v > 0]
        fair_avg = sum(mods) / len(mods) if mods else np.nan
        entree_conseillee = fair_avg * 0.85 if not pd.isna(fair_avg) else np.nan
        return entree_conseillee, entree_bna
    except Exception:
        return np.nan, np.nan


def tdm_get_advanced_market_data(tickers, compute_entry_price=False):
    """
    Version optimisée : les champs principaux (prix, variation, capitalisation,
    PER, P/B, dividende, actions en circulation, bourse...) viennent d'appels
    groupés à l'API "quote" de Yahoo (`_yahoo_quote_batch`) au lieu d'un
    `yf.Ticker(t).info` par titre — c'était le vrai goulot d'étranglement de
    cette page (boucle séquentielle, un appel réseau lent par titre, sans
    parallélisation). L'historique de prix ET les dividendes YTD sont
    téléchargés en un seul appel groupé (`yf.download(..., actions=True)`),
    ce qui évite un appel `.dividends` individuel par titre.
    Seul le calcul optionnel "Entrée Conseillée" (compute_entry_price=True)
    nécessite encore un accès par titre (cash-flow indisponible en lot chez
    Yahoo) ; il est désormais parallélisé au lieu d'être séquentiel.
    Repli individuel (`.info`) uniquement pour les titres absents du lot Yahoo.
    """
    tickers = list(tickers)
    if not tickers:
        return pd.DataFrame()

    debut_annee = f"{datetime.now().year}-01-01"

    df_prices = yf.download(
        tickers, start=debut_annee, progress=False, auto_adjust=False,
        actions=True,
    )
    if isinstance(df_prices.columns, pd.MultiIndex):
        df_prices.columns = df_prices.columns.remove_unused_levels()

    def _hist_serie(t, champ):
        try:
            if isinstance(df_prices.columns, pd.MultiIndex):
                return df_prices[champ, t].dropna() if (champ, t) in df_prices.columns else pd.Series(dtype=float)
            return df_prices[champ].dropna() if champ in df_prices.columns else pd.Series(dtype=float)
        except Exception:
            return pd.Series(dtype=float)

    # --- Un seul appel groupé (par lots) pour l'essentiel des champs fondamentaux ---
    quotes = _yahoo_quote_batch(tickers)

    # Repli individuel — en parallèle — uniquement pour les titres absents du lot.
    manquants = [t for t in tickers if t not in quotes]
    infos_repli = {}
    if manquants:
        def _one_info(t):
            try:
                return t, yf.Ticker(t).info
            except Exception:
                return t, {}
        with ThreadPoolExecutor(max_workers=20) as executor:
            for t, info in executor.map(_one_info, manquants):
                infos_repli[t] = info

    # --- Entrée conseillée : uniquement si demandé, calculée en parallèle ---
    entrees = {}
    if compute_entry_price:
        def _one_entree(t):
            info_t = quotes.get(t) or infos_repli.get(t, {})
            return t, _tdm_calc_entree_conseillee(t, info_t)
        with ThreadPoolExecutor(max_workers=15) as executor:
            for t, res in executor.map(_one_entree, tickers):
                entrees[t] = res

    rows = []
    for t in tickers:
        try:
            q = quotes.get(t) or {}
            info_repli = infos_repli.get(t, {})

            def _champ(nom, alt=None):
                v = q.get(nom)
                if v is None and alt is not None:
                    v = q.get(alt)
                if v is None:
                    v = info_repli.get(nom) if info_repli.get(nom) is not None else (
                        info_repli.get(alt) if alt else None)
                return v

            close_series = _hist_serie(t, "Close")
            open_series  = _hist_serie(t, "Open")
            vol_series   = _hist_serie(t, "Volume")
            div_series   = _hist_serie(t, "Dividends")

            if close_series.empty:
                continue

            p_ytd_start = float(close_series.iloc[0])

            live_price = _champ("regularMarketPrice", "currentPrice")
            p_latest = float(live_price) if live_price else float(close_series.iloc[-1])

            prev_close_live = _champ("regularMarketPreviousClose", "previousClose")
            p_prev_close = float(prev_close_live) if prev_close_live else (
                float(close_series.iloc[-2]) if len(close_series) > 1 else p_latest)

            open_today_live = _champ("regularMarketOpen")
            p_open_today = float(open_today_live) if open_today_live else (
                float(open_series.iloc[-1]) if not open_series.empty else p_latest)

            volume_actuel = int(vol_series.iloc[-1]) if not vol_series.empty else 0
            volume_moyen = vol_series.mean() if not vol_series.empty else 0

            montant_actuel = p_latest * volume_actuel
            montant_moyen = (close_series * vol_series).mean() if not vol_series.empty else 0

            reg_time = _champ("regularMarketTime")
            if reg_time and isinstance(reg_time, (int, float)):
                timestamp_latest = datetime.fromtimestamp(reg_time).strftime('%Y-%m-%d %H:%M:%S')
            elif not close_series.empty:
                timestamp_latest = close_series.index[-1].strftime('%Y-%m-%d 16:00:00')
            else:
                timestamp_latest = ""

            div_rate = _champ("dividendRate")
            if div_rate and isinstance(div_rate, (int, float)) and p_latest > 0:
                dividend_yield = (div_rate / p_latest) * 100
            else:
                div_yield_raw = _champ("dividendYield")
                if div_yield_raw is not None and not pd.isna(div_yield_raw):
                    dividend_yield = div_yield_raw if div_yield_raw > 1.0 else div_yield_raw * 100
                else:
                    dividend_yield = np.nan

            # Dividendes détachés depuis le 1er janvier (déjà présents dans
            # l'historique groupé grâce à actions=True, plus d'appel .dividends
            # individuel nécessaire).
            dividends_ytd = float(div_series.sum()) if not div_series.empty else 0.0

            # Bénéfice TTM : approximé via EPS TTM x actions en circulation
            # (les deux disponibles dans le lot Yahoo), pour éviter un appel
            # .info individuel rien que pour ce champ. Repli sur netIncomeToCommon
            # (.info) si l'approximation n'est pas calculable.
            eps_ttm = _champ("epsTrailingTwelveMonths")
            shares_out = _champ("sharesOutstanding")
            if eps_ttm is not None and shares_out is not None:
                profit_ttm = float(eps_ttm) * float(shares_out)
            else:
                profit_ttm = info_repli.get("netIncomeToCommon", np.nan)

            entree_conseillee, entree_bna = entrees.get(t, (np.nan, np.nan))

            rows.append({
                "Ticker": t,
                "Name": _champ("longName", "shortName") or t,
                "Currency": _champ("currency") or 'USD',
                "Price": p_prev_close,
                "IntradayReturn": ((p_latest - p_prev_close) / p_prev_close) * 100,
                "Price_Latest": p_latest,
                "Return_Latest": ((p_latest - p_open_today) / p_open_today) * 100 if p_open_today != 0 else 0,
                "EntreeConseillee_Raw": entree_conseillee,
                "EntreeBNA_Raw": entree_bna,

                "Volume_Raw": volume_actuel,
                "Volume_Moyen_Raw": volume_moyen,
                "Amount_Raw": montant_actuel,
                "Amount_Moyen_Raw": montant_moyen,

                "MarketCap_Raw": _champ("marketCap") or 0,
                "YTDReturn": ((p_latest - p_ytd_start) / p_ytd_start) * 100,
                "YTDReturnTotal": ((p_latest + dividends_ytd - p_ytd_start) / p_ytd_start) * 100,
                "PE": _champ("trailingPE") or np.nan,
                "PB": _champ("priceToBook") or np.nan,
                "Profit_TTM_Raw": profit_ttm,
                "DividendYield": dividend_yield,
                "Dividend": div_rate if div_rate else np.nan,
                "SharesOutstanding_Raw": shares_out if shares_out is not None else np.nan,
                "Exchange": _champ("fullExchangeName", "exchange") or 'N/A',
                "Timestamp_Latest": timestamp_latest
            })
        except Exception:
            continue

    df = pd.DataFrame(rows)
    if df.empty: return df

    # Taux de change vers l'euro pour chaque devise de cotation présente dans l'indice.
    # NB : indispensable pour que "Poids" / "Influence Intraday" / "Contrib. YTD" restent
    # corrects sur les indices multi-devises (MSCI World, MSCI Emerging Markets...) — sommer
    # des capitalisations en INR + JPY + USD sans conversion faussait ces calculs.
    fx_rates = tdm_get_fx_rates(tuple(df['Currency'].fillna('USD').unique()))
    df['FXRate'] = df['Currency'].fillna('USD').map(fx_rates).fillna(1.0)

    df['MarketCap_EUR'] = df['MarketCap_Raw'] * df['FXRate']
    total_mcap_eur = df['MarketCap_EUR'].sum()
    df['Weight'] = (df['MarketCap_EUR'] / total_mcap_eur) * 100 if total_mcap_eur > 0 else 0
    df['IntradayContribution'] = (df['Weight'] * df['IntradayReturn']) / 100
    df['YTDContribution'] = (df['Weight'] * df['YTDReturn']) / 100

    # Valeurs numériques brutes conservées (formatage K/M/B à l'affichage uniquement, cf. Styler.format)
    # pour permettre un tri numérique correct dans st.dataframe au lieu d'un tri alphabétique sur du texte.
    # Chaque montant existe en deux versions : devise locale de cotation (ex. "Capitalisation")
    # et convertie en euros (ex. "Capitalisation_EUR"), pour basculer entre les deux à l'affichage.
    df['Volume'] = df['Volume_Raw']
    df['VolumeMoyen'] = df['Volume_Moyen_Raw']
    df['Amount'] = df['Amount_Raw']
    df['AmountMoyen'] = df['Amount_Moyen_Raw']
    df['Amount_EUR'] = df['Amount_Raw'] * df['FXRate']
    df['AmountMoyen_EUR'] = df['Amount_Moyen_Raw'] * df['FXRate']

    df['MarketCap'] = df['MarketCap_Raw']
    df['SharesOutstanding'] = df['SharesOutstanding_Raw']
    df['EntreeConseillee'] = df['EntreeConseillee_Raw']
    df['EntreeBNA'] = df['EntreeBNA_Raw']
    df['EntreeConseillee_EUR'] = df['EntreeConseillee_Raw'] * df['FXRate']
    df['EntreeBNA_EUR'] = df['EntreeBNA_Raw'] * df['FXRate']

    df['Price_EUR'] = df['Price'] * df['FXRate']
    df['PriceLatest_EUR'] = df['Price_Latest'] * df['FXRate']
    df['Dividend_EUR'] = df['Dividend'] * df['FXRate']
    df['Profit_TTM'] = df['Profit_TTM_Raw']
    df['ProfitTTM_EUR'] = df['Profit_TTM_Raw'] * df['FXRate']

    ordered_cols = [
        "Ticker", "Name", "Currency", "Weight", "Price", "Price_EUR", "IntradayReturn", "Price_Latest", "PriceLatest_EUR",
        "EntreeConseillee", "EntreeConseillee_EUR", "EntreeBNA", "EntreeBNA_EUR", "Return_Latest",
        "Volume", "VolumeMoyen", "Amount", "AmountMoyen", "Amount_EUR", "AmountMoyen_EUR",
        "IntradayContribution", "MarketCap", "MarketCap_EUR", "YTDReturn", "YTDReturnTotal", "YTDContribution",
        "PE", "PB", "Profit_TTM", "ProfitTTM_EUR",
        "DividendYield", "Dividend", "Dividend_EUR", "SharesOutstanding", "Exchange", "Timestamp_Latest",
        "Volume_Raw", "Volume_Moyen_Raw", "Amount_Raw", "Amount_Moyen_Raw",
        "EntreeConseillee_Raw", "EntreeBNA_Raw"
    ]
    df = df[ordered_cols]

    fr_cols = {
        "Ticker": "Ticker", "Name": "Nom", "Currency": "Devise", "Weight": "Poids", "Price": "Prix Veille",
        "Price_EUR": "Prix Veille (€)",
        "IntradayReturn": "Var. Intraday (%)", "Price_Latest": "Dernier Prix", "PriceLatest_EUR": "Dernier Prix (€)",
        "EntreeConseillee": "Entrée Conseillée", "EntreeConseillee_EUR": "Entrée Conseillée (€)",
        "EntreeBNA": "Entrée BNA -15%", "EntreeBNA_EUR": "Entrée BNA -15% (€)",
        "Return_Latest": "Var. Session (%)",
        "Volume": "Volume", "VolumeMoyen": "Volume Moyen",
        "Amount": "Montant", "AmountMoyen": "Montant Moyen",
        "Amount_EUR": "Montant (€)", "AmountMoyen_EUR": "Montant Moyen (€)",
        "IntradayContribution": "Influence Intraday",
        "MarketCap": "Capitalisation", "MarketCap_EUR": "Capitalisation (€)",
        "YTDReturn": "Var. YTD (%)", "YTDReturnTotal": "Var. YTD Totale, div. incl. (%)", "YTDContribution": "Contrib. YTD", "PE": "PER",
        "PB": "P/B", "Profit_TTM": "Bénéfice TTM", "ProfitTTM_EUR": "Bénéfice TTM (€)", "DividendYield": "Rend. Dividende",
        "Dividend": "Dividende", "Dividend_EUR": "Dividende (€)", "SharesOutstanding": "Actions en Circ.",
        "Exchange": "Bourse", "Timestamp_Latest": "Dernier Horodatage",
        "EntreeConseillee_Raw": "EntreeConseillee_Raw",
        "EntreeBNA_Raw": "EntreeBNA_Raw"
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
    show_entry_price = st.sidebar.checkbox(
        "💰 Afficher prix conseillé",
        value=False,
        key="tdm_show_entry_price",
        help="Calcule un prix d'entrée théorique par valeur (moyenne des modèles BNA/FCF/Analystes -15%). "
             "Ralentit le chargement du tableau car cela nécessite une requête supplémentaire par action."
    )
    convert_eur = st.sidebar.checkbox(
        "🔁 Convertir les montants en €",
        value=False,
        key="tdm_convert_eur",
        help="Convertit Prix / Montant / Capitalisation / Dividende / Bénéfice dans la devise locale de "
             "cotation de chaque titre (par défaut, une colonne 'Devise' l'indique) vers l'euro, au taux de "
             "change actuel — utile pour comparer des indices cotés dans des devises différentes (ex. NIFTY 50 "
             "en roupies, Nikkei en yens)."
    )

    index_daily_changes = tdm_get_index_daily_changes()

    def _label_avec_variation(nom):
        pct = index_daily_changes.get(nom)
        if pct is None:
            return nom
        emoji = "🟢" if pct >= 0 else "🔴"
        return f"{nom}  {emoji} {pct:+.2f}%"

    st.sidebar.divider()
    compare_mode = st.sidebar.checkbox(
        "🔀 Comparer plusieurs indices",
        value=False,
        key="tdm_compare_mode",
        help="Superpose la performance (%) de plusieurs indices sur un même graphique"
    )

    compare_selection = []
    if compare_mode:
        TDM_COMPARE_PRESETS = {
            "Indices majeurs": ["CAC 40 (France)", "S&P 500 Lite (USA)", "DAX (Allemagne)", "NASDAQ 100 Tech"],
            "Europe": ["CAC 40 (France)", "DAX (Allemagne)", "FTSE 100 (Royaume-Uni)",
                       "EURO STOXX 50", "FTSE MIB (Italie)"],
            "Marchés émergents": ["MSCI Emerging Markets", "NIFTY 50 (Inde)", "FTSE China 50"],
            "Effacer": [],
        }

        with st.sidebar.container(border=True):
            st.markdown('<div class="tdm-compare-title">📊 Indices à comparer</div>', unsafe_allow_html=True)

            preset_cols = st.columns(2)
            for i, (label, tickers_preset) in enumerate(TDM_COMPARE_PRESETS.items()):
                if preset_cols[i % 2].button(label, key=f"tdm_preset_{i}", width='stretch'):
                    st.session_state["tdm_compare_selection"] = [
                        n for n in tickers_preset if n in TDM_INDEX_TICKER_MAP
                    ]
                    st.rerun()

            default_compare = [
                n for n in ["CAC 40 (France)", "S&P 500 Lite (USA)", "DAX (Allemagne)",
                            "NASDAQ 100 Tech", "MSCI World"]
                if n in TDM_INDEX_TICKER_MAP
            ]
            compare_selection = st.multiselect(
                "Sélection",
                options=sorted(TDM_INDEX_TICKER_MAP.keys()),
                default=default_compare,
                key="tdm_compare_selection",
                label_visibility="collapsed",
                placeholder="Choisir des indices à comparer…"
            )
            st.caption(f"{len(compare_selection)} indice(s) sélectionné(s)")
        st.sidebar.divider()

    selected_index = st.sidebar.radio(
        "Indices disponibles :" if not compare_mode else "Indice pour le tableau détaillé :",
        sorted(TDM_INDEX_TICKER_MAP.keys()),
        key="tdm_selected_index",
        format_func=_label_avec_variation
    )
    current_tickers = tdm_resolve_index_tickers(selected_index)

    st.title("📈 Tableau de Bord d'Indicateurs Financiers")

    spinner_msg = "Chargement des données..."
    if len(current_tickers) > 60:
        spinner_msg = (f"Chargement des données pour les {len(current_tickers)} valeurs de l'indice "
                        f"(composition réelle via Wikipedia) — cela peut prendre une minute ou plus...")

    with st.spinner(spinner_msg):
        df_data = tdm_get_advanced_market_data(current_tickers, show_entry_price)

    if show_charts and compare_mode:
        if not compare_selection:
            st.info("👈 Sélectionnez au moins un indice à comparer dans le menu latéral.")
        else:
            x_title = "Date" if show_ytd else "Heure"
            titre_periode = "Comparaison de performance — Année en cours (YTD)" if show_ytd \
                else "Comparaison de performance — Intraday"

            with st.spinner("Chargement des données de comparaison..."):
                data_dict = {}
                for nom in compare_selection:
                    sym = TDM_SYMBOL_MAP.get(nom)
                    if not sym:
                        continue
                    data_dict[nom] = tdm_get_ytd_data(sym) if show_ytd else tdm_get_intraday_data(sym)

            st.markdown(f"#### {titre_periode}")

            kpi_cols = st.columns(len(compare_selection))
            for col, nom in zip(kpi_cols, compare_selection):
                df_i = data_dict.get(nom)
                if df_i is not None and not df_i.empty:
                    base = float(df_i['Close'].iloc[0])
                    last = float(df_i['Close'].iloc[-1])
                    pct = ((last - base) / base) * 100 if base else 0
                    color = "#006622" if pct >= 0 else "#cc0000"
                    with col:
                        st.markdown(
                            f"""<div style="text-align:center;">
                                <div style="font-size:13px; font-weight:600;">{nom}</div>
                                <div style="font-size:18px; font-weight:800; color:{color};">{pct:+.2f}%</div>
                            </div>""",
                            unsafe_allow_html=True
                        )
                else:
                    with col:
                        st.markdown(
                            f"""<div style="text-align:center;">
                                <div style="font-size:13px; font-weight:600;">{nom}</div>
                                <div style="font-size:13px; color:#999;">N/A</div>
                            </div>""",
                            unsafe_allow_html=True
                        )

            fig_compare = _tdm_build_comparison_chart(
                data_dict,
                f"Comparaison d'indices — {titre_periode}",
                x_title
            )
            st.plotly_chart(
                fig_compare,
                width='stretch',
                config={
                    'scrollZoom': True,
                    'displayModeBar': True,
                    'modeBarButtonsToAdd': ['drawline', 'drawrect', 'eraseshape'],
                    'displaylogo': False
                }
            )

    elif show_charts:
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
                width='stretch',
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
                    # Les deux versions (devise locale / €) existent toujours dans df_data ;
                    # on colorie les deux, seule celle réellement affichée sera visible.
                    style_df.loc[idx, 'Montant'] = 'color: #cc0000;'
                    style_df.loc[idx, 'Montant (€)'] = 'color: #cc0000;'
            return style_df

        def style_entry_opportunity(df, price_col, conseillee_col, bna_col):
            """Style du nom de l'action et des colonnes d'entrée selon les seuils d'entrée :
            - vert + gras si le Dernier Prix < Entrée Conseillée ;
            - en plus, fond vert clair (signal renforcé) si le Dernier Prix < Entrée Conseillée
              ET < Entrée BNA -15% (les deux confirment). Comparaison faite dans la même devise
              (locale ou € selon le mode d'affichage actif) pour rester cohérente.
            - la valeur de 'Entrée Conseillée' est elle-même colorée en vert si elle est
              supérieure au Dernier Prix, idem pour 'Entrée BNA -15%'."""
            style_df = pd.DataFrame('', index=df.index, columns=df.columns)
            if {conseillee_col, bna_col, price_col}.issubset(df.columns):
                below_conseillee = df[conseillee_col].notna() & (df[price_col] < df[conseillee_col])
                below_bna = df[bna_col].notna() & (df[price_col] < df[bna_col])

                mask_double = below_conseillee & below_bna

                style_df.loc[below_conseillee, 'Nom'] = 'color: #28a745; font-weight: bold;'
                style_df.loc[mask_double, 'Nom'] = (
                    'color: #28a745; font-weight: bold; background-color: #e6ffec;'
                )

                # Colore la valeur elle-même dans les colonnes "Entrée Conseillée" / "Entrée BNA -15%"
                # si elle est supérieure au Dernier Prix.
                style_df.loc[below_conseillee, conseillee_col] = 'color: #28a745;'
                style_df.loc[below_bna, bna_col] = 'color: #28a745;'
            return style_df

        df_styled = df_data.style\
            .map(style_market_colors, subset=['Var. Intraday (%)', 'Var. Session (%)', 'Var. YTD (%)', 'Var. YTD Totale, div. incl. (%)'])\
            .apply(style_volume_amount_alerts, axis=None)\
            .bar(subset=['Poids'], color='#4a90e2', vmin=0, vmax=float(df_data['Poids'].max()))

        if show_entry_price:
            price_col = "Dernier Prix (€)" if convert_eur else "Dernier Prix"
            conseillee_col = "Entrée Conseillée (€)" if convert_eur else "Entrée Conseillée"
            bna_col = "Entrée BNA -15% (€)" if convert_eur else "Entrée BNA -15%"
            df_styled = df_styled.apply(
                lambda d: style_entry_opportunity(d, price_col, conseillee_col, bna_col), axis=None
            )

        df_styled = df_styled.format({
                'Prix Veille': '{:.2f}',
                'Dernier Prix': '{:.2f}',
                'Entrée Conseillée': '{:.2f}',
                'Entrée BNA -15%': '{:.2f}',
                'Prix Veille (€)': '{:.2f} €',
                'Dernier Prix (€)': '{:.2f} €',
                'Entrée Conseillée (€)': '{:.2f} €',
                'Entrée BNA -15% (€)': '{:.2f} €',
                'Var. Intraday (%)': '{:+.2f}%',
                'Var. Session (%)': '{:+.2f}%',
                'Poids': '{:.2f}%',
                'Influ. Intraday': '{:+.2f}%',
                'Var. YTD (%)': '{:+.2f}%',
                'Var. YTD Totale, div. incl. (%)': '{:+.2f}%',
                'Influ. YTD': '{:+.2f}%',
                'PER': '{:.2f}',
                'P/B': '{:.2f}',
                'Rend. Dividende': '{:.2f}%',
                'Dividende': '{:.2f}',
                'Dividende (€)': '{:.2f} €',
                'Bénéfice TTM': lambda v: f"{v / 1e9:.2f} B" if pd.notna(v) else "N/A",
                'Bénéfice TTM (€)': lambda v: f"{v / 1e9:.2f} B €" if pd.notna(v) else "N/A",
                'Capitalisation': lambda v: f"{v / 1e9:.2f} B" if pd.notna(v) else "N/A",
                'Capitalisation (€)': lambda v: f"{v / 1e9:.2f} B €" if pd.notna(v) else "N/A",
                'Volume': lambda v: f"{int(round(v / 1e3))}K" if pd.notna(v) else "N/A",
                'Volume Moyen': lambda v: f"{int(round(v / 1e3))}K" if pd.notna(v) else "N/A",
                'Montant': lambda v: f"{int(round(v / 1e6))}M" if pd.notna(v) else "N/A",
                'Montant Moyen': lambda v: f"{int(round(v / 1e6))}M" if pd.notna(v) else "N/A",
                'Montant (€)': lambda v: f"{int(round(v / 1e6))}M €" if pd.notna(v) else "N/A",
                'Montant Moyen (€)': lambda v: f"{int(round(v / 1e6))}M €" if pd.notna(v) else "N/A",
                'Actions en Circ.': lambda v: _tdm_format_smart_large_numbers(v) if pd.notna(v) else "N/A"
            }, na_rep="N/A")

        if convert_eur:
            cols_to_display = [
                "Ticker", "Nom", "Poids", "Prix Veille (€)", "Var. Intraday (%)", "Dernier Prix (€)",
                "Var. Session (%)", "Volume", "Volume Moyen", "Montant (€)", "Montant Moyen (€)",
                "Influence Intraday", "Capitalisation (€)", "Var. YTD (%)", "Var. YTD Totale, div. incl. (%)", "Contrib. YTD",
                "PER", "P/B", "Bénéfice TTM (€)", "Rend. Dividende", "Dividende (€)", "Actions en Circ.",
                "Bourse", "Dernier Horodatage"
            ]
            if show_entry_price:
                idx_insert = cols_to_display.index("Dernier Prix (€)") + 1
                cols_to_display.insert(idx_insert, "Entrée BNA -15% (€)")
                cols_to_display.insert(idx_insert, "Entrée Conseillée (€)")
        else:
            cols_to_display = [
                "Ticker", "Nom", "Devise", "Poids", "Prix Veille", "Var. Intraday (%)", "Dernier Prix",
                "Var. Session (%)", "Volume", "Volume Moyen", "Montant", "Montant Moyen",
                "Influence Intraday", "Capitalisation", "Var. YTD (%)", "Var. YTD Totale, div. incl. (%)", "Contrib. YTD",
                "PER", "P/B", "Bénéfice TTM", "Rend. Dividende", "Dividende", "Actions en Circ.",
                "Bourse", "Dernier Horodatage"
            ]
            if show_entry_price:
                idx_insert = cols_to_display.index("Dernier Prix") + 1
                cols_to_display.insert(idx_insert, "Entrée BNA -15%")
                cols_to_display.insert(idx_insert, "Entrée Conseillée")

        caption_txt = "💡 Cliquez sur une ligne pour afficher la fiche détaillée de l'action."
        if not convert_eur:
            caption_txt += " 🌍 Les montants sont dans la devise locale de cotation de chaque titre (colonne 'Devise')."
        if show_entry_price:
            caption_txt += (" 🟢 Nom en vert et gras si le dernier prix est inférieur à l'Entrée Conseillée ; "
                             "en plus surligné en vert clair si l'Entrée BNA -15% le confirme également.")
        st.caption(caption_txt)

        df_export = df_data[cols_to_display].copy()
        nom_fichier = selected_index.split(" (")[0].replace(" ", "_").replace("&", "et")

        col_export1, col_export2, _col_export_spacer = st.columns([1, 1, 4])
        with col_export1:
            st.download_button(
                "📥 Exporter en CSV",
                data=df_export.to_csv(index=False, sep=';').encode('utf-8-sig'),
                file_name=f"{nom_fichier}_composition.csv",
                mime="text/csv",
                width='stretch',
                key="tdm_export_csv"
            )
        with col_export2:
            try:
                excel_buffer = io.BytesIO()
                with pd.ExcelWriter(excel_buffer, engine='openpyxl') as writer:
                    df_export.to_excel(writer, index=False, sheet_name='Composition')
                st.download_button(
                    "📥 Exporter en Excel",
                    data=excel_buffer.getvalue(),
                    file_name=f"{nom_fichier}_composition.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    width='stretch',
                    key="tdm_export_xlsx"
                )
            except Exception:
                pass

        nb_lignes_marche = len(df_data)
        st.caption(f"{nb_lignes_marche} valeur{'s' if nb_lignes_marche != 1 else ''}")

        sel_marche = st.dataframe(
            df_styled,
            width='stretch',
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

    /* --- Comparaison d'indices : présentation "pro" --- */
    div[data-testid="stMultiSelect"] span[data-baseweb="tag"] {
        background-color: #1f3a5f !important;
        border-radius: 6px !important;
        padding: 2px 4px !important;
    }
    div[data-testid="stMultiSelect"] span[data-baseweb="tag"] span {
        color: #f5f7fa !important;
        font-weight: 500 !important;
        font-size: 12.5px !important;
    }
    div[data-testid="stMultiSelect"] span[data-baseweb="tag"] svg {
        fill: #cbd5e1 !important;
    }
    .tdm-compare-card {
        border: 1px solid #e2e8f0;
        border-radius: 10px;
        padding: 14px 14px 10px 14px;
        background-color: #fafbfc;
        margin-bottom: 4px;
    }
    .tdm-compare-title {
        font-size: 14px;
        font-weight: 700;
        color: #1f3a5f;
        margin-bottom: 6px;
    }
    div[data-testid="stSidebar"] div[data-testid="stButton"] button {
        font-size: 12px !important;
        padding: 2px 8px !important;
        border-radius: 14px !important;
        border: 1px solid #cbd5e1 !important;
        background-color: #ffffff !important;
        color: #334155 !important;
    }
    div[data-testid="stSidebar"] div[data-testid="stButton"] button:hover {
        border-color: #1f3a5f !important;
        color: #1f3a5f !important;
    }
    </style>
""", unsafe_allow_html=True)

# =======================================================================
# BARRE D'HEURES DISCRÈTE (Paris / NYSE-NASDAQ) — MàJ live côté client (JS)
# =======================================================================

def afficher_barre_horaires_bourses():
    components.html(
        """
        <div id="mc-bar" style="
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            font-size: 12px;
            color: #64748b;
            background: #f8fafc;
            border: 1px solid #e2e8f0;
            border-radius: 8px;
            padding: 5px 12px;
            margin-bottom: 6px;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 18px;
            flex-wrap: wrap;
            line-height: 1.4;
        ">
          <span id="mc-paris"></span>
          <span style="color:#cbd5e1;">|</span>
          <span id="mc-ny"></span>
          <span style="color:#cbd5e1;">|</span>
          <span id="mc-tokyo"></span>
          <span style="color:#cbd5e1;">|</span>
          <span id="mc-india"></span>
        </div>
        <script>
        function mcPad(n) { return n.toString().padStart(2, '0'); }

        function mcZonedParts(tz) {
            const now = new Date();
            const fmt = new Intl.DateTimeFormat('en-US', {
                timeZone: tz, hour12: false,
                hour: '2-digit', minute: '2-digit', second: '2-digit',
                weekday: 'short'
            });
            const obj = {};
            fmt.formatToParts(now).forEach(p => obj[p.type] = p.value);
            return {
                h: parseInt(obj.hour, 10),
                m: parseInt(obj.minute, 10),
                s: parseInt(obj.second, 10),
                weekday: obj.weekday
            };
        }

        function mcIsOpen(parts, openH, openM, closeH, closeM) {
            if (['Sat', 'Sun'].includes(parts.weekday)) return false;
            const nowMin = parts.h * 60 + parts.m;
            return nowMin >= (openH * 60 + openM) && nowMin < (closeH * 60 + closeM);
        }

        function mcTick() {
            const paris = mcZonedParts('Europe/Paris');
            const ny = mcZonedParts('America/New_York');
            const tokyo = mcZonedParts('Asia/Tokyo');
            const india = mcZonedParts('Asia/Kolkata');

            const parisOpen = mcIsOpen(paris, 9, 0, 17, 30);
            const nyOpen = mcIsOpen(ny, 9, 30, 16, 0);
            // TSE : 2 séances avec pause déjeuner (9h-11h30 / 12h30-15h heure de Tokyo)
            const tokyoOpen = mcIsOpen(tokyo, 9, 0, 11, 30) || mcIsOpen(tokyo, 12, 30, 15, 0);
            // NSE/BSE : 9h15-15h30 heure de l'Inde
            const indiaOpen = mcIsOpen(india, 9, 15, 15, 30);

            document.getElementById('mc-paris').innerHTML =
                '🇫🇷 Paris ' + mcPad(paris.h) + ':' + mcPad(paris.m) + ':' + mcPad(paris.s) +
                ' · Euronext ' + (parisOpen ? '🟢 Ouvert' : '🔴 Fermé') +
                ' <span style="color:#94a3b8;">(9h–17h30)</span>';

            document.getElementById('mc-ny').innerHTML =
                '🇺🇸 New York ' + mcPad(ny.h) + ':' + mcPad(ny.m) + ':' + mcPad(ny.s) +
                ' · NYSE/NASDAQ ' + (nyOpen ? '🟢 Ouvert' : '🔴 Fermé') +
                ' <span style="color:#94a3b8;">(9h30–16h)</span>';

            document.getElementById('mc-tokyo').innerHTML =
                '🇯🇵 Tokyo ' + mcPad(tokyo.h) + ':' + mcPad(tokyo.m) + ':' + mcPad(tokyo.s) +
                ' · TSE ' + (tokyoOpen ? '🟢 Ouvert' : '🔴 Fermé') +
                ' <span style="color:#94a3b8;">(9h-11h30 / 12h30-15h)</span>';

            document.getElementById('mc-india').innerHTML =
                '🇮🇳 Mumbai ' + mcPad(india.h) + ':' + mcPad(india.m) + ':' + mcPad(india.s) +
                ' · NSE/BSE ' + (indiaOpen ? '🟢 Ouvert' : '🔴 Fermé') +
                ' <span style="color:#94a3b8;">(9h15–15h30)</span>';
        }

        mcTick();
        setInterval(mcTick, 1000);
        </script>
        """,
        height=40,
    )


afficher_barre_horaires_bourses()

# --- MENU DE NAVIGATION (SIDEBAR) ---
with st.sidebar:
    page_actuelle = st.radio(
        "🧭 Navigation",
        ["📁 Portefeuille", "🌍 Indices", "📰 Actualités", "🔎 Recherche"],
        index=2,
        key="page_actuelle",
    )
    st.divider()

show_news_portfolio = (page_actuelle == "📰 Actualités")

# =======================================================================
# PAGE : RECHERCHE UNITAIRE D'UNE ACTION
# =======================================================================
if page_actuelle == "🔎 Recherche":
    afficher_menu_indices()

    if st.session_state.get("vue_indice"):
        st.divider()
        afficher_dashboard_indice(st.session_state["vue_indice"])
        st.stop()

    if st.session_state.get("vue_etf"):
        st.divider()
        afficher_dashboard_etf(st.session_state["vue_etf"])
        st.stop()

    st.divider()
    st.header("🔎 Recherche unitaire d'une action")
    st.caption("Recherchez une valeur pour retrouver sa fiche détaillée (diagnostic santé financière, "
               "performance & volumes, entrée conseillée, actualités) sans avoir à l'ajouter à un portefeuille.")

    rq = st.text_input("Nom de la société ou ticker (ex: Brenntag, BNR.DE, LVMH)", key="recherche_unitaire_q")

    if rq:
        rsug = search_ticker(rq)
        if rsug:
            ropt = [x['label'] for x in rsug]
            rsel_opt = st.selectbox("Résultats :", ropt, key="recherche_unitaire_sel")
            r_ticker = rsug[ropt.index(rsel_opt)]['symbol']

            with st.spinner(f"Analyse détaillée de {r_ticker}..."):
                r_detail = fetch_stock_data(r_ticker)

            st.divider()
            if r_detail:
                afficher_detail_action(r_detail)
            else:
                st.info("Données fondamentales indisponibles pour cette valeur.")
        else:
            st.warning("Aucun résultat trouvé pour cette recherche.")
    st.stop()

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
    st.markdown("""
        <style>
        .sb-mini-header {
            font-size: 0.92em; font-weight: 700; color: #1f3a5f;
            margin: 2px 0 6px 0; display:flex; align-items:center; gap:6px;
        }
        .sb-thin-divider { border: none; border-top: 1px solid #e2e8f0; margin: 10px 0; }
        .ticker-card {
            display:flex; justify-content:space-between; align-items:center;
            padding:8px 12px; margin-bottom:6px; border-radius:9px;
            background-color:#f5f7fa; border:1px solid #e5e9f0;
            border-left: 3px solid #1f3a5f;
        }
        .ticker-card .nom { font-weight:700; color:#1f3a5f; font-size:0.95em; }
        .ticker-card .tk {
            font-size:0.75em; color:#64748b; background:white; padding:2px 7px;
            border-radius:5px; border:1px solid #e2e8f0; white-space:nowrap; margin-left:8px;
        }
        </style>
    """, unsafe_allow_html=True)

    # Correspondance affichage : renomme certains profils sans toucher au Google Sheet source
    LABELS_PROFILS = {"Dividendes": "Div. et Résultats"}

    try:
        df_conf      = get_column_config()
        liste_profils = sorted(df_conf['Profil'].unique().tolist())
    except Exception:
        df_conf = None
        liste_profils = []

    if show_news_portfolio:
        profil_choisi = liste_profils[0] if liste_profils else None
    else:
        st.markdown('<div class="sb-mini-header">📋 Vue</div>', unsafe_allow_html=True)
        if liste_profils:
            liste_profils_affiche = [LABELS_PROFILS.get(p, p) for p in liste_profils]
            map_affiche_to_reel  = {LABELS_PROFILS.get(p, p): p for p in liste_profils}
            profil_affiche = st.selectbox(
                "Vue :", options=liste_profils_affiche, label_visibility="collapsed", key="profil_vue_choisi"
            )
            profil_choisi = map_affiche_to_reel.get(profil_affiche, profil_affiche)
        else:
            profil_choisi = None
        st.markdown('<hr class="sb-thin-divider">', unsafe_allow_html=True)

    st.markdown('<div class="sb-mini-header">📂 Portefeuilles</div>', unsafe_allow_html=True)
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

    st.markdown('<hr class="sb-thin-divider">', unsafe_allow_html=True)

    current_content = load_watchlist_gsheets(sel_list)
    if "ticker_editor" not in st.session_state:
        st.session_state["ticker_editor"] = current_content

    show_edit_tickers = st.toggle("✏️ Modifier", key="show_edit_tickers",
                                   help="Afficher l'éditeur pour modifier les tickers de ce portefeuille")

    tickers_actuels = [t.strip().upper() for t in current_content.replace('\r', '').replace('\n', ',').split(',') if t.strip()]

    if tickers_actuels:
        with ThreadPoolExecutor(max_workers=min(20, len(tickers_actuels))) as executor:
            noms_tickers = dict(zip(tickers_actuels, executor.map(get_action_name, tickers_actuels)))
    else:
        noms_tickers = {}

    if show_edit_tickers:
        st.caption(f"✏️ Édition de « {sel_list} » — {len(tickers_actuels)} valeur(s)")

        if tickers_actuels:
            for tk in tickers_actuels:
                c1, c2 = st.columns([0.82, 0.18])
                with c1:
                    st.markdown(
                        f"<div class='ticker-card' style='margin-bottom:4px;'>"
                        f"<span class='nom'>{noms_tickers.get(tk, tk)}</span>"
                        f"<span class='tk'>{tk}</span></div>",
                        unsafe_allow_html=True
                    )
                with c2:
                    if st.button("✕", key=f"del_tk_{tk}", help=f"Retirer {tk} du portefeuille"):
                        nouvelle_liste = [x for x in tickers_actuels if x != tk]
                        nouveau_contenu = ", ".join(nouvelle_liste)
                        save_watchlist_gsheets(sel_list, nouveau_contenu)
                        st.session_state["ticker_editor"] = nouveau_contenu
                        st.cache_data.clear()
                        st.rerun()
        else:
            st.info("Aucun ticker dans cette liste pour l'instant.")

        st.markdown("**➕ Ajouter une valeur**")
        aq = st.text_input("Nom ou ticker", key="edit_add_search", label_visibility="collapsed",
                            placeholder="Ex: LVMH, AAPL...")
        if aq:
            asug = search_ticker(aq)
            if asug:
                aopt  = [x['label'] for x in asug]
                asel  = st.selectbox("Résultats :", aopt, key="edit_add_select", label_visibility="collapsed")
                a_tk  = asug[aopt.index(asel)]['symbol']
                if st.button(f"➕ Ajouter {a_tk}", key="edit_add_btn", width="stretch"):
                    if a_tk not in tickers_actuels:
                        nouvelle_liste = tickers_actuels + [a_tk]
                        nouveau_contenu = ", ".join(nouvelle_liste)
                        save_watchlist_gsheets(sel_list, nouveau_contenu)
                        st.session_state["ticker_editor"] = nouveau_contenu
                        st.cache_data.clear()
                        st.rerun()
                    else:
                        st.warning(f"{a_tk} est déjà dans la liste.")
            else:
                st.caption("Aucun résultat pour cette recherche.")

        with st.expander("⚙️ Édition avancée (texte brut)"):
            tickers_input = st.text_area(
                "Éditer les tickers :",
                value=current_content,
                height=100,
                key="ticker_editor",
                on_change=update_tickers_callback
            ).upper()
    else:
        tickers_input = st.session_state.get("ticker_editor", current_content)
        if tickers_actuels:
            st.caption(f"{len(tickers_actuels)} valeur(s) dans « {sel_list} »")
            for tk in tickers_actuels:
                st.markdown(
                    f"<div class='ticker-card'>"
                    f"<span class='nom'>{noms_tickers.get(tk, tk)}</span>"
                    f"<span class='tk'>{tk}</span></div>",
                    unsafe_allow_html=True
                )
        else:
            st.info("Aucun ticker dans cette liste. Cliquez sur « Modifier » pour en ajouter.")

    st.markdown('<hr class="sb-thin-divider">', unsafe_allow_html=True)


    # Colonnes disponibles (sans les colonnes internes)
    cols_all = [
        "Nom", "Secteur", "Prix Actuel",
        "BNA Actuel", "PER Actuel", "PEG Actuel", "PEG Forward",
        "BNA Forward", "PER Forward",
        "ROA", "ROE", "Marge Nette", "Dette/Equity", "Beta",
        "Croissance EBITDA", "P/FCF", "P/FCF Moy 3a",   # NOUVEAU
        "CAGR 3 ans", "CAGR 5 ans",
        "Entrée BNA -15%", "Source PER (BNA)", "Entrée FCF -15%", "Entrée Analystes -15%", "Entrée Synthèse (-15%)",
        "Santé (Piotroski)",
        "Chg 1J", "Chg 1M", "Chg YTD", "Chg YTD (div. incl.)",
        "Nb Analystes", "Dividende (€/$)", "Rendement %", "Date Détachement",
        "Date Versement Dividende", "Prochains Résultats", "Avis Analystes"
    ]

# =======================================================================
# VUE INDICE (si un indice est sélectionné dans la sidebar)
# =======================================================================
if st.session_state.get("vue_indice"):
    afficher_dashboard_indice(st.session_state["vue_indice"])
    st.stop()

if st.session_state.get("vue_etf"):
    afficher_dashboard_etf(st.session_state["vue_etf"])
    st.stop()

# --- TITRE ---
col_titre, col_refresh_titre = st.columns([0.92, 0.08])
with col_titre:
    st.title(f"📁 Portefeuille : {sel_list}")
with col_refresh_titre:
    if not show_news_portfolio:
        st.write("")
        st.write("")
        if st.button("🔄", help="Forcer l'actualisation des données", key="btn_force_refresh_titre"):
            st.cache_data.clear()
            st.rerun()
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
        # format='mixed' : les dates viennent de sources hétérogènes (formats non uniformes
        # selon les titres/API) ; sans le préciser, pandas retombe sur un parsing élément par
        # élément via dateutil (lent + UserWarning à chaque exécution). 'mixed' garde le même
        # comportement (dayfirst respecté, valeurs invalides -> NaT) mais sans l'avertissement.
        df['Date Détachement'] = pd.to_datetime(df['Date Détachement'], errors='coerce', dayfirst=True, format='mixed')
        if 'Date Versement Dividende' in df.columns:
            df['Date Versement Dividende'] = pd.to_datetime(df['Date Versement Dividende'], errors='coerce', dayfirst=True, format='mixed')
        if 'Prochains Résultats' in df.columns:
            df['Prochains Résultats'] = pd.to_datetime(df['Prochains Résultats'], errors='coerce', dayfirst=True, format='mixed')
        ticker_to_name = dict(zip(df['Ticker'], df['Nom']))

        # Colonnes internes à masquer du tableau
        COLS_INTERNES = {'p_details', 'full_data'}

        try:
            config_active = df_conf[df_conf['Profil'] == profil_choisi]
            cols_base         = config_active[config_active['Afficher'] == True]['Nom_Colonne'].tolist()
            cols_figees_base  = config_active[config_active['Figer'] == True]['Nom_Colonne'].tolist()
            # Vue "Div. et Résultats" (ex-"Dividendes") : on force l'ajout de la colonne
            # "Prochains Résultats" même si elle n'est pas cochée dans le Google Sheet source.
            if profil_choisi == "Dividendes" and "Prochains Résultats" not in cols_base:
                cols_base.append("Prochains Résultats")
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

            for col in ['Chg 1J', 'Chg 1M', 'Chg YTD', 'Chg YTD (div. incl.)']:
                if col in df.columns:
                    styles[col] = 'text-align: left;'
                    col_num = pd.to_numeric(df[col], errors='coerce')
                    mask_plus  = col_num > 0
                    mask_moins = col_num < 0
                    styles.loc[mask_plus,  col] += ' color: #28a745; font-weight: bold;'
                    styles.loc[mask_moins, col] += ' color: #dc3545; font-weight: bold;'

            # Dividende / résultats imminents (< 1 semaine) : mise en avant rose foncé
            aujourd_hui = pd.Timestamp.now().normalize()
            for col_date in ['Date Versement Dividende', 'Prochains Résultats']:
                if col_date in df.columns:
                    dates_col   = pd.to_datetime(df[col_date], errors='coerce')
                    delta_jours = (dates_col - aujourd_hui).dt.days
                    mask_imminent = delta_jours.notna() & (delta_jours >= 0) & (delta_jours <= 7)
                    styles.loc[mask_imminent, col_date] += 'background-color: #fce4ec; color: #ad1457; font-weight: bold;'

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
            def _fmt_chg(x):
                if x is None or (isinstance(x, float) and pd.isna(x)):
                    return "N/A"
                return f"{'📈' if x > 0 else '📉'} {x:+.2f}%"

            chg_cols_present = [c for c in ['Chg 1J', 'Chg 1M', 'Chg YTD', 'Chg YTD (div. incl.)'] if c in selection_finale]

            # Streamlit aligne à droite par défaut les colonnes de type numérique, même si le
            # formatter du Styler affiche du texte. On force ces colonnes en TextColumn pour
            # obtenir un véritable alignement à gauche.
            config_colonnes_affichage = {
                "Date Détachement": st.column_config.DateColumn("Date Détachement", format="DD/MM/YYYY"),
                "Date Versement Dividende": st.column_config.DateColumn("Date Versement Dividende", format="DD/MM/YYYY"),
                "Prochains Résultats": st.column_config.DateColumn("Prochains Résultats", format="DD/MM/YYYY"),
            }
            for col in chg_cols_present:
                config_colonnes_affichage[col] = st.column_config.NumberColumn(
                    col, alignment="left", pinned=(col in selection_figee)
                )
            for col in selection_figee:
                if col not in config_colonnes_affichage:
                    config_colonnes_affichage[col] = st.column_config.Column(pinned=True)

            sel = st.dataframe(
                df[selection_finale].style.apply(style_df, axis=None).format(
                    formatter=lambda x: clean_num(x) if isinstance(x, (int, float)) else x
                ).format(
                    formatter=_fmt_chg, subset=chg_cols_present
                ),
                on_select="rerun",
                selection_mode="single-row",
                width="stretch",
                hide_index=True,
                height=min(hauteur_dynamique, 850),
                column_config=config_colonnes_affichage,
            )

            # =======================================================================
            # VUE DÉTAIL (ligne sélectionnée)
            # =======================================================================

            if sel.selection and sel.selection.rows:
                d = data_res[sel.selection.rows[0]]
                afficher_detail_action(d)