import streamlit as st
import yfinance as yf
import pandas as pd
import os
import requests
from datetime import datetime

# --- 1. CONFIGURATION & DOSSIERS ---
WATCHLIST_DIR = "watchlists"
COLUMNS_FILE = "columns_config.txt"

# Création propre du dossier
if not os.path.exists(WATCHLIST_DIR):
    os.makedirs(WATCHLIST_DIR)

# Création du fichier par défaut uniquement s'il n'y a RIEN du tout
if not os.path.exists(os.path.join(WATCHLIST_DIR, "Portefeuille Principal.txt")):
    with open(os.path.join(WATCHLIST_DIR, "Portefeuille Principal.txt"), "w", encoding="utf-8") as f:
        f.write("SAN.PA, AI.PA, TTE.PA, AAPL, MSFT")

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
        
        div_date = info.get("exDividendDate")
        div_date_str = datetime.fromtimestamp(div_date).strftime('%d/%m/%Y') if div_date else "N/A"
        
        return {
            "Ticker": ticker_str, "Nom": info.get("longName", ticker_str),
            "Secteur": SECTORS_FR.get(info.get("sector"), info.get("sector")),
            "Prix Actuel": p, "BNA Actuel": info.get("trailingEps", 0), "PER Actuel": info.get("trailingPE", 0),
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
    return sorted([f.replace(".txt", "") for f in os.listdir(WATCHLIST_DIR) if f.endswith(".txt")])

def load_watchlist(name):
    path = os.path.join(WATCHLIST_DIR, f"{name}.txt")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return ""

def save_watchlist(name, content):
    # On s'assure d'utiliser le dossier des watchlists
    filepath = os.path.join(WATCHLIST_DIR, f"{name}.txt")
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    st.success(f"✅ Liste '{name}' sauvegardée !")

def load_columns(all_cols):
    if os.path.exists("selected_columns.txt"):
        try:
            # On force l'encodage ET on gère les erreurs de lecture
            with open("selected_columns.txt", "r", encoding="utf-8") as f:
                saved = f.read().split(",")
                return [c for c in saved if c in all_cols]
        except Exception:
            # Si le fichier est illisible, on ne plante pas, on renvoie les défauts
            return all_cols[:5]
    return all_cols[:5]

# --- 5. INTERFACE ---
st.set_page_config(page_title="Expert Bourse Pro+", layout="wide")
st.markdown("<style>.block-container {padding-top: 1rem;} [data-testid='stTable'] {font-size: 13px;}</style>", unsafe_allow_html=True)

with st.sidebar:
    st.header("🔍 Recherche d'Action")
    sq = st.text_input("Nom de la société (ex: LVMH)")
    if sq:
        sug = search_ticker(sq)
        if sug:
            opt = [x['label'] for x in sug]
            sel_opt = st.selectbox("Résultats :", opt)
            tk_add = sug[opt.index(sel_opt)]['symbol']
            if st.button(f"➕ Ajouter {tk_add}"):
                cur_tk = load_watchlist(st.session_state.get('sel_list', 'Portefeuille Principal'))
                save_watchlist(st.session_state.get('sel_list', 'Portefeuille Principal'), (cur_tk + f", {tk_add}") if cur_tk else tk_add)
                st.rerun()

    st.divider()
    
    # --- ÉTAPE A : CRÉER (Pour ajouter un nouveau fichier) ou supprimer ---
    st.header("📂 Portefeuilles")
    lists = get_all_watchlists()
    sel_list = st.selectbox("Liste active :", lists, key='sel_list')

    # --- OPTIONS DE GESTION (Tiroirs) ---
    col1, col2 = st.columns(2)
    with col1:
        show_add = st.toggle("➕ Créer")
    with col2:
        show_del = st.toggle("🗑️ Supprimer")

    # Logique d'Ajout
    if show_add:
        st.info("Créer une nouvelle liste")
        new_name = st.text_input("Nom de la liste :", placeholder="Ex: Dividendes")
        if st.button("Confirmer Création", use_container_width=True):
            if new_name:
                save_watchlist(new_name, "AAPL")
                st.success(f"'{new_name}' créée !")
                st.rerun()
            else:
                st.error("Nom vide !")

    # Logique de Suppression
    if show_del:
        st.warning(f"⚠️ Action irréversible")
        list_to_del = st.selectbox("Choisir la liste à supprimer :", lists, key="del_select_box")
        
        # On ajoute une clé unique au bouton de suppression
        if st.button(f"Confirmer la suppression de {list_to_del}", type="primary", key="btn_confirm_del"):
            if len(lists) > 1:
                # Utilise bien le nom du dossier défini en haut de ton script
                filepath = os.path.join("watchlists", f"{list_to_del}.txt")
                
                if os.path.exists(filepath):
                    try:
                        os.remove(filepath)
                        st.success(f"🔥 Liste '{list_to_del}' supprimée avec succès !")
                        # Pause d'une demi-seconde pour laisser l'utilisateur voir le message
                        import time
                        time.sleep(0.5)
                        st.rerun()
                    except Exception as e:
                        st.error(f"Erreur lors de la suppression : {e}")
                else:
                    st.error(f"Fichier introuvable : {filepath}")
            else:
                st.error("🚫 Impossible de supprimer la dernière liste !")

    st.divider()
    # --- ÉTAPE B : ÉDITER & SAUVEGARDER (Ton code actuel) ---
    # On charge le contenu de la liste sélectionnée
    current_content = load_watchlist(sel_list)

    tickers_input = st.text_area("Éditer les tickers :", value=load_watchlist(sel_list), height=100).upper()
    if st.button("💾 Sauver Liste"): save_watchlist(sel_list, tickers_input)

    st.divider()
    cols_all = ["Nom", "Secteur", "Prix Actuel", "BNA Actuel", "PER Actuel", "BNA Forward", "PER Forward", "Nb Analystes", 
                "Entrée BNA -15%", "Entrée FCF -15%", "Entrée Analystes -15%", "Entrée Synthèse (-15%)", "Santé (Piotroski)", "Dividende (€/$)", "Rendement %", "Date Détachement", "Avis Analystes"]
    sel_cols = st.multiselect("Colonnes :", cols_all, default=load_columns(cols_all))
    if st.button("💾 Sauver Colonnes"):
        with open(COLUMNS_FILE, "w", encoding="utf-8") as f:
            f.write(",".join(sel_cols))
        st.success("Configuration des colonnes sauvegardée !")

st.title(f"📈 {sel_list}")
# Cette ligne est "blindée" contre les espaces, les sauts de ligne et les minuscules
t_list = [t.strip().upper() for t in tickers_input.replace('\r', '').replace('\n', ',').split(',') if t.strip()]

if t_list:
# REMPLACEMENT DES LIGNES 211-213
    data_res = []
    for t in t_list:
        res = fetch_stock_data(t)
        if res:
            data_res.append(res)
    
    if data_res:
        df = pd.DataFrame(data_res)
        
        with st.sidebar:
            st.divider()
            csv = df.drop(columns=['p_details', 'full_data']).to_csv(index=False, sep=';', encoding='utf-8-sig')
            st.download_button("📥 Télécharger CSV", data=csv, file_name=f"Watchlist_{sel_list}.csv")

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
            
            return styles

        sel = st.dataframe(
            df[["Ticker"] + sel_cols].style.apply(style_df, axis=None).format(formatter=lambda x: clean_num(x) if isinstance(x, (int, float)) else x),
            on_select="rerun", selection_mode="single-row", use_container_width=True, hide_index=True, height="content"
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