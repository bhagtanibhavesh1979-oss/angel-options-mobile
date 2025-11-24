import flet as ft
import requests
import pyotp
import math
import threading
import time
import os
import json
from datetime import datetime, timedelta, time as dt_time

# --- THEME COLORS ---
BG_COLOR = "#000000"        # Pure Black
CARD_COLOR = "#121212"      # OLED Dark Grey
TEXT_WHITE = "#FFFFFF"
TEXT_GREY = "#9E9E9E"
ACCENT_GREEN = "#00E676"    # Profit/Call
ACCENT_RED = "#FF5252"      # Loss/Put
ACCENT_BLUE = "#2979FF"     # Info
ACCENT_GOLD = "#FFC107"     # Fair Value Highlight

# --- CONSTANTS ---
API_BASE = "https://apiconnect.angelbroking.com/rest"
CACHE_FILE = "angel_master_calc.json" 

INSTRUMENTS = {
    "NIFTY": ("NSE", "99926000"),
    "BANKNIFTY": ("NSE", "99926009"),
    "FINNIFTY": ("NSE", "99926037"),
    "SENSEX": ("BSE", "99919000"),
    "MIDCPNIFTY": ("NSE", "99926074")
}

# --- MATH ENGINE (PURE PYTHON) ---
PI = 3.141592653589793

def norm_pdf(x):
    return (1.0 / math.sqrt(2 * PI)) * math.exp(-0.5 * x * x)

def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def d1_d2(S, K, T, r, sigma):
    if T <= 0 or S <= 0 or K <= 0 or sigma <= 0: return None, None
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return d1, d2

def black_scholes_price(S, K, T, r, sigma, option_type="CE"):
    if T <= 0: return max(0.0, S - K) if option_type == "CE" else max(0.0, K - S)
    d1, d2 = d1_d2(S, K, T, r, sigma)
    if d1 is None: return 0.0
    if option_type == "CE":
        return S * norm_cdf(d1) - K * math.exp(-r * T) * norm_cdf(d2)
    else:
        return K * math.exp(-r * T) * norm_cdf(-d2) - S * norm_cdf(-d1)

def calculate_delta(S, K, T, r, sigma, option_type="CE"):
    d1, _ = d1_d2(S, K, T, r, sigma)
    if d1 is None: return 0.0
    if option_type == "CE": return norm_cdf(d1)
    else: return norm_cdf(d1) - 1.0

def calculate_implied_volatility(price, S, K, T, r, option_type="CE"):
    v = 0.5 
    for i in range(5): 
        p = black_scholes_price(S, K, T, r, v, option_type)
        d1, _ = d1_d2(S, K, T, r, v)
        if d1 is None: break
        vega = S * math.sqrt(T) * norm_pdf(d1)
        diff = price - p
        if abs(diff) < 0.01: return v
        if abs(vega) < 0.00001: break 
        v = v + diff / vega
    return max(0.01, v)

# --- STATE MANAGEMENT ---
class AppState:
    jwt_token = None
    headers = None
    master_data = []
    logged_in = False
    auto_refresh = False
    
    # SETTINGS
    risk_free_rate = 0.10
    model_iv = 0.15
    strike_count = 6  # Default 6 above, 6 below (13 rows total)
    alert_threshold = 5.0

state = AppState()

# --- API HELPERS ---
def login_angel(api_key, client_code, pin, totp_secret):
    try:
        totp = pyotp.TOTP(totp_secret).now()
        url = f"{API_BASE}/auth/angelbroking/user/v1/loginByPassword"
        payload = {"clientcode": client_code, "password": pin, "totp": totp}
        headers = {"Content-Type": "application/json", "Accept": "application/json", "X-UserType": "USER", "X-SourceID": "WEB", "X-ClientLocalIP": "127.0.0.1", "X-ClientPublicIP": "127.0.0.1", "X-MACAddress": "00-00-00-00-00-00", "X-PrivateKey": api_key}
        r = requests.post(url, json=payload, headers=headers, timeout=10)
        data = r.json()
        if data.get("status") and data.get("data", {}).get("jwtToken"): return data["data"]["jwtToken"]
    except: pass
    return None

def get_headers(api_key, jwt_token):
    return {"Content-Type": "application/json", "Accept": "application/json", "X-UserType": "USER", "X-SourceID": "WEB", "X-ClientLocalIP": "127.0.0.1", "X-ClientPublicIP": "127.0.0.1", "X-MACAddress": "00-00-00-00-00-00", "X-PrivateKey": api_key, "Authorization": f"Bearer {jwt_token}"}

def load_token_master(log_func=None):
    if os.path.exists(CACHE_FILE):
        try:
            file_time = datetime.fromtimestamp(os.path.getmtime(CACHE_FILE))
            if datetime.now() - file_time < timedelta(hours=12):
                if log_func: log_func("Loading Cache...")
                with open(CACHE_FILE, 'r') as f: return json.load(f)
        except: pass

    try:
        if log_func: log_func("Downloading Master (50MB)...")
        url = "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
        r = requests.get(url, timeout=90) 
        data = r.json()
        optimized = []
        allowed_names = set(INSTRUMENTS.keys())
        for item in data:
            itype = item.get('instrumenttype')
            name = item.get('name')
            if (itype == 'OPTIDX' or itype == 'OPTSTK') and name in allowed_names:
                item['token'] = str(item.get('token', '')).split('.')[0].strip()
                item['exch_seg'] = str(item.get('exch_seg', '')).upper().strip()
                try: item['strike_real'] = float(item.get('strike', 0)) / 100.0
                except: item['strike_real'] = 0.0
                optimized.append(item)
        with open(CACHE_FILE, 'w') as f: json.dump(optimized, f)
        return optimized
    except Exception as e: 
        if log_func: log_func(f"Err: {e}")
        return []

def get_spot_price(symbol):
    val = INSTRUMENTS.get(symbol)
    if not val: return 0.0
    exch, token = val
    url = f"{API_BASE}/secure/angelbroking/market/v1/quote/"
    payload = {"mode": "LTP", "exchangeTokens": {exch: [token]}}
    try:
        r = requests.post(url, json=payload, headers=state.headers, timeout=5)
        d = r.json()
        if d.get("status"):
            ltp = float(d['data']['fetched'][0]['ltp'])
            if symbol in ["NIFTY", "FINNIFTY", "MIDCPNIFTY"] and ltp > 100000: ltp /= 10
            elif symbol == "BANKNIFTY" and ltp > 200000: ltp /= 10
            elif symbol == "SENSEX" and ltp > 200000: ltp /= 10
            return ltp
    except: pass
    return 0.0

def get_batch_quotes(tokens, exchange="NFO"):
    results = {}
    chunk_size = 20
    str_tokens = [str(t).strip() for t in tokens]
    for i in range(0, len(str_tokens), chunk_size):
        chunk = str_tokens[i:i + chunk_size]
        url = f"{API_BASE}/secure/angelbroking/market/v1/quote/"
        payload = {"mode": "FULL", "exchangeTokens": {exchange: chunk}}
        try:
            r = requests.post(url, json=payload, headers=state.headers, timeout=3)
            d = r.json()
            if d.get("status") and 'fetched' in d['data']:
                for item in d['data']['fetched']:
                    t = item.get('symbolToken')
                    ltp = float(item.get('ltp', 0))
                    if ltp == 0: ltp = float(item.get('close', 0))
                    if ltp > 50000: ltp /= 100.0 
                    if t: results[t] = ltp
        except: pass
    return results

def get_expiries(symbol):
    if not state.master_data: return []
    exps = set()
    for item in state.master_data:
        if item.get('name') == symbol: exps.add(item.get('expiry'))
    exps_list = list(exps)
    try: exps_list.sort(key=lambda x: datetime.strptime(x, "%d%b%Y"))
    except: pass
    today = datetime.now().date()
    valid = []
    for e in exps_list:
        try:
            if datetime.strptime(e, "%d%b%Y").date() >= today: valid.append(e)
        except: pass
    return valid[:6] if valid else exps_list[-5:]

def get_chain_data(symbol, expiry, spot):
    """
    Returns a symmetric list of tokens around the Spot price.
    Eg: 5 strikes below ATM, ATM, 5 strikes above ATM.
    """
    if spot == 0 or not expiry: return []
    
    # 1. Get all strikes for this expiry
    candidates = []
    for item in state.master_data:
        if item.get('name') == symbol and item.get('expiry') == expiry:
            candidates.append(item)
    
    if not candidates: return []

    # 2. Extract unique strikes and sort
    unique_strikes = sorted(list(set(x['strike_real'] for x in candidates)))
    if not unique_strikes: return []

    # 3. Find ATM Index (Closest to Spot)
    atm_index = 0
    min_diff = float('inf')
    for i, s in enumerate(unique_strikes):
        diff = abs(s - spot)
        if diff < min_diff:
            min_diff = diff
            atm_index = i
            
    # 4. Slice symmetrically
    count = state.strike_count
    start_idx = max(0, atm_index - count)
    end_idx = min(len(unique_strikes), atm_index + count + 1)
    
    selected_strikes = unique_strikes[start_idx:end_idx]

    # 5. Filter the original candidates to just these strikes
    final_data = [x for x in candidates if x['strike_real'] in selected_strikes]
    final_data.sort(key=lambda x: x['strike_real'])
    
    return final_data

# --- MAIN APP ---
def main(page: ft.Page):
    page.title = "Angel Pro"
    page.bgcolor = BG_COLOR
    page.theme_mode = "dark"
    page.padding = 0 
    
    # --- GLOBAL UI ---
    progress_bar = ft.ProgressBar(width=None, color=ACCENT_BLUE, bgcolor="#333333", visible=False)
    
    def show_snack(msg, color=ACCENT_GREEN):
        page.snack_bar = ft.SnackBar(ft.Text(msg, color="black", weight="bold"), bgcolor=color)
        page.snack_bar.open = True
        page.update()

    def show_alert(title, msg):
        dlg = ft.AlertDialog(
            title=ft.Text(title),
            content=ft.Text(msg),
            actions=[ft.TextButton("OK", on_click=lambda e: page.close(dlg))]
        )
        page.open(dlg)

    # --- LOGIN SCREEN ---
    api_input = ft.TextField(label="API Key", password=True, text_size=12, border_color=TEXT_GREY)
    client_input = ft.TextField(label="Client Code", text_size=12, border_color=TEXT_GREY)
    pin_input = ft.TextField(label="PIN", password=True, text_size=12, border_color=TEXT_GREY)
    totp_input = ft.TextField(label="TOTP Code", text_size=12, border_color=TEXT_GREY)
    login_btn = ft.ElevatedButton("Secure Login", bgcolor=ACCENT_BLUE, color="white", height=45)
    login_status = ft.Text("", color=ACCENT_GOLD, size=12)

    # --- HOME / CHAIN SCREEN ---
    spot_display = ft.Text("0.00", size=24, weight="bold", color=TEXT_WHITE)
    spot_label = ft.Text("SPOT", size=10, color=TEXT_GREY)
    
    idx_dd = ft.Dropdown(
        options=[ft.dropdown.Option(x) for x in INSTRUMENTS.keys()],
        value="NIFTY", text_size=14, border_color=TEXT_GREY, 
        bgcolor=CARD_COLOR, content_padding=5, expand=True
    )
    exp_dd = ft.Dropdown(
        text_size=14, border_color=TEXT_GREY, 
        bgcolor=CARD_COLOR, content_padding=5, expand=True
    )
    refresh_btn = ft.IconButton(icon="refresh", icon_color=ACCENT_BLUE, bgcolor=CARD_COLOR)
    auto_switch = ft.Switch(label="Auto", value=False, active_color=ACCENT_GREEN)

    # UPDATED TABLE COLUMNS AS REQUESTED
    # Order: CE LTP | Fair | Strike | PE LTP | Fair | IV | Delta
    chain_table = ft.DataTable(
        columns=[
            ft.DataColumn(ft.Text("CE LTP", color=TEXT_WHITE, size=11, weight="bold")),
            ft.DataColumn(ft.Text("Fair", color=ACCENT_GOLD, size=11)),
            ft.DataColumn(ft.Text("Strike", color=TEXT_WHITE, weight="bold", size=12)),
            ft.DataColumn(ft.Text("PE LTP", color=TEXT_WHITE, size=11, weight="bold")),
            ft.DataColumn(ft.Text("Fair", color=ACCENT_GOLD, size=11)),
            ft.DataColumn(ft.Text("IV (C|P)", color=TEXT_GREY, size=10)),
            ft.DataColumn(ft.Text("Δ (C|P)", color=TEXT_GREY, size=10)),
        ],
        column_spacing=10, # Compact spacing
        data_row_min_height=40,
        heading_row_height=40,
        horizontal_lines=ft.border.BorderSide(0.5, "#333333"),
        rows=[]
    )

    # --- CALCULATOR COMPONENTS ---
    calc_type = ft.Dropdown(options=[ft.dropdown.Option("CE"), ft.dropdown.Option("PE")], value="CE", label="Type", width=100, bgcolor=CARD_COLOR)
    calc_spot = ft.TextField(label="Target Spot Price", value="24000", text_size=14, border_color=ACCENT_BLUE)
    calc_strike = ft.TextField(label="Strike Price", value="24000", text_size=14, border_color=TEXT_GREY)
    calc_days = ft.TextField(label="Days Left", value="1", width=100, border_color=TEXT_GREY)
    calc_iv = ft.TextField(label="IV %", value="15", width=100, border_color=TEXT_GREY)
    calc_res_price = ft.Text("₹0.00", size=30, weight="bold", color=ACCENT_GOLD)
    
    def calc_click(e):
        try:
            S = float(calc_spot.value)
            K = float(calc_strike.value)
            T = float(calc_days.value) / 365.0
            r = state.risk_free_rate
            v = float(calc_iv.value) / 100.0
            op = calc_type.value
            fair = black_scholes_price(S, K, T, r, v, op)
            calc_res_price.value = f"₹{fair:.2f}"
            page.update()
        except: pass

    # --- SETTINGS COMPONENTS ---
    set_rfr = ft.TextField(label="Risk Free Rate (%)", value=str(state.risk_free_rate*100), width=150)
    set_iv = ft.TextField(label="Fair Model IV (%)", value=str(state.model_iv*100), width=150)
    set_alert = ft.TextField(label="Disc. Alert (%)", value=str(state.alert_threshold), width=150)
    set_strikes = ft.TextField(label="Strike Count", value=str(state.strike_count), width=150)

    # --- LOGIC ---
    def update_expiries(e=None):
        exps = get_expiries(idx_dd.value)
        exp_dd.options = [ft.dropdown.Option(x) for x in exps]
        if exps: exp_dd.value = exps[0]
        page.update()

    def refresh_chain(e):
        if not state.master_data: return
        progress_bar.visible = True
        spot_display.color = TEXT_GREY
        page.update()

        spot = get_spot_price(idx_dd.value)
        spot_display.value = f"₹{spot}"
        spot_display.color = TEXT_WHITE
        
        if not exp_dd.value: 
            progress_bar.visible = False
            page.update()
            return

        chain = get_chain_data(idx_dd.value, exp_dd.value, spot)
        if not chain:
            progress_bar.visible = False
            page.update()
            return

        nfo = [x['token'] for x in chain if x['exch_seg'] == 'NFO']
        bfo = [x['token'] for x in chain if x['exch_seg'] == 'BFO']
        quotes = {}
        if nfo: quotes.update(get_batch_quotes(nfo, 'NFO'))
        if bfo: quotes.update(get_batch_quotes(bfo, 'BFO'))

        rows = []
        opportunities = []
        
        try:
            ed = datetime.strptime(exp_dd.value, "%d%b%Y")
            T = max((datetime.combine(ed.date(), dt_time(15, 30)) - datetime.now()).total_seconds() / 31536000, 0.0001)
        except: T = 0.01
        
        try: 
            r_rate = float(set_rfr.value) / 100.0
            m_iv = float(set_iv.value) / 100.0
            alert_th = float(set_alert.value) / 100.0
        except: 
            r_rate, m_iv, alert_th = 0.10, 0.15, 0.05

        strikes = sorted(list(set(x['strike_real'] for x in chain)))
        step = 50
        if len(strikes)>1: step = strikes[1]-strikes[0]

        for K in strikes:
            ce = next((x for x in chain if x['strike_real'] == K and "CE" in x['symbol']), None)
            pe = next((x for x in chain if x['strike_real'] == K and "PE" in x['symbol']), None)
            
            ltp_c = quotes.get(ce['token'], 0) if ce else 0
            ltp_p = quotes.get(pe['token'], 0) if pe else 0
            
            fair_c = black_scholes_price(spot, K, T, r_rate, m_iv, "CE")
            fair_p = black_scholes_price(spot, K, T, r_rate, m_iv, "PE")
            
            delta_c = calculate_delta(spot, K, T, r_rate, m_iv, "CE")
            delta_p = calculate_delta(spot, K, T, r_rate, m_iv, "PE")
            
            iv_c = calculate_implied_volatility(ltp_c, spot, K, T, r_rate, "CE") if ltp_c > 0 else 0
            iv_p = calculate_implied_volatility(ltp_p, spot, K, T, r_rate, "PE") if ltp_p > 0 else 0

            # COLORS
            c_col = TEXT_WHITE
            if ltp_c > 0 and fair_c > ltp_c * (1 + alert_th): 
                c_col = ACCENT_GREEN
                opportunities.append(f"CALL {int(K)}")
            
            p_col = TEXT_WHITE
            if ltp_p > 0 and fair_p > ltp_p * (1 + alert_th): 
                p_col = ACCENT_GREEN 
                opportunities.append(f"PUT {int(K)}")

            # Highlight ATM
            is_atm = abs(spot - K) < (step / 1.8)
            bg = "#212121" if is_atm else None

            rows.append(ft.DataRow(
                color=bg,
                cells=[
                    ft.DataCell(ft.Text(f"{ltp_c:.2f}", color=c_col, weight="bold", size=12)),
                    ft.DataCell(ft.Text(f"{fair_c:.0f}", color=ACCENT_GOLD, size=12)), 
                    ft.DataCell(ft.Text(str(int(K)), weight="bold", size=13, color=TEXT_WHITE)),
                    ft.DataCell(ft.Text(f"{ltp_p:.2f}", color=p_col, weight="bold", size=12)),
                    ft.DataCell(ft.Text(f"{fair_p:.0f}", color=ACCENT_GOLD, size=12)), 
                    
                    # Compact IV and Delta
                    ft.DataCell(ft.Text(f"{int(iv_c*100)} | {int(iv_p*100)}", color=TEXT_GREY, size=11)),
                    ft.DataCell(ft.Text(f"{delta_c:.2f} | {delta_p:.2f}", color=ACCENT_BLUE, size=11)),
                ]
            ))

        chain_table.rows = rows
        progress_bar.visible = False
        
        if opportunities and not state.auto_refresh:
            show_alert("Discount Alert!", f"Found valuable options:\n{', '.join(opportunities[:5])}")
        elif opportunities:
            show_snack(f"Found {len(opportunities)} Opportunities!", ACCENT_GREEN)
            
        page.update()

    def auto_loop():
        while state.auto_refresh:
            refresh_chain(None)
            time.sleep(10)
            
    def toggle_auto(e):
        state.auto_refresh = auto_switch.value
        if state.auto_refresh: threading.Thread(target=auto_loop, daemon=True).start()
    auto_switch.on_change = toggle_auto

    def save_settings(e):
        try:
            state.risk_free_rate = float(set_rfr.value) / 100.0
            state.model_iv = float(set_iv.value) / 100.0
            state.alert_threshold = float(set_alert.value)
            state.strike_count = int(set_strikes.value)
            show_snack("Settings Saved")
        except: show_snack("Invalid Settings", ACCENT_RED)

    def login_click(e):
        login_status.value = "Connecting..."
        progress_bar.visible = True
        page.update()
        t = login_angel(api_input.value, client_input.value, pin_input.value, totp_input.value)
        if t:
            state.jwt_token = t
            state.headers = get_headers(api_input.value, t)
            login_status.value = "Downloading Master..."
            page.update()
            d = load_token_master()
            if d:
                state.master_data = d
                state.logged_in = True
                idx_dd.on_change = update_expiries
                refresh_btn.on_click = refresh_chain
                body.content = tab_home
                page.add(nav_bar)
                update_expiries()
            else:
                login_status.value = "Master File Error"
        else:
            login_status.value = "Login Failed"
        progress_bar.visible = False
        page.update()
    login_btn.on_click = login_click

    # --- LAYOUTS ---
    tab_login = ft.Container(
        content=ft.Column([
            ft.Icon(name="lock_clock", size=60, color=ACCENT_BLUE),
            ft.Text("Angel Pro", size=28, weight="bold"),
            ft.Container(height=20),
            api_input, client_input, pin_input, totp_input,
            ft.Container(height=10),
            login_btn, login_status, progress_bar
        ], alignment="center", horizontal_alignment="center"),
        padding=30, alignment=ft.alignment.center
    )

    tab_home = ft.Container(
        content=ft.Column([
            progress_bar,
            ft.Container(
                content=ft.Row([
                    ft.Column([spot_label, spot_display], spacing=0),
                    ft.Column([auto_switch, refresh_btn], spacing=0, alignment="end")
                ], alignment="spaceBetween"),
                padding=10, bgcolor=CARD_COLOR, border_radius=10
            ),
            ft.Row([idx_dd, exp_dd], spacing=10),
            # HORIZONTAL SCROLL FOR TABLE
            ft.Container(
                content=ft.Row([chain_table], scroll=ft.ScrollMode.ALWAYS),
                expand=True
            )
        ]),
        padding=10, expand=True
    )

    tab_calc = ft.Container(
        content=ft.Column([
            ft.Text("Black-Scholes Calculator", size=20, weight="bold"),
            ft.Text("Estimate option price based on future spot", size=11, color=TEXT_GREY),
            ft.Divider(),
            ft.Row([calc_type, calc_days, calc_iv]),
            calc_spot,
            calc_strike,
            ft.ElevatedButton("Calculate Fair Price", on_click=calc_click, bgcolor=ACCENT_BLUE, color="white"),
            ft.Container(height=20),
            ft.Text("Fair Value:", size=12, color=TEXT_GREY),
            calc_res_price
        ], horizontal_alignment="center"),
        padding=20, expand=True
    )

    tab_settings = ft.Container(
        content=ft.Column([
            ft.Text("Settings", size=20, weight="bold"),
            ft.Divider(),
            ft.Row([set_rfr, set_iv], alignment="spaceBetween"),
            ft.Text("RFR = Risk Free Rate | IV = Model Volatility", size=10, color=TEXT_GREY),
            ft.Container(height=10),
            ft.Row([set_alert, set_strikes], alignment="spaceBetween"),
            ft.Text("Alert = Discount % for Green Signal", size=10, color=TEXT_GREY),
            ft.Text("Strikes = Count above/below Spot", size=10, color=TEXT_GREY),
            ft.Container(height=20),
            ft.ElevatedButton("Save Changes", on_click=save_settings, bgcolor=ACCENT_BLUE, color="white")
        ]),
        padding=20, expand=True
    )

    body = ft.Container(content=tab_login, expand=True) 

    def nav_click(e):
        data = e.control.data
        if data == "Chain": body.content = tab_home
        elif data == "Calc": body.content = tab_calc
        elif data == "Settings": body.content = tab_settings
        page.update()

    nav_bar = ft.Container(
        content=ft.Row([
            ft.IconButton(icon="home", icon_color=ACCENT_BLUE, data="Chain", on_click=nav_click),
            ft.IconButton(icon="calculate", icon_color=ACCENT_BLUE, data="Calc", on_click=nav_click),
            ft.IconButton(icon="settings", icon_color=ACCENT_BLUE, data="Settings", on_click=nav_click),
        ], alignment="spaceAround"),
        bgcolor=CARD_COLOR,
        padding=5,
        border_radius=ft.border_radius.only(top_left=15, top_right=15)
    )

    page.add(body)

ft.app(target=main)
