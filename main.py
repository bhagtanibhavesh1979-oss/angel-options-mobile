import flet as ft
import requests
import pyotp
import math
import threading
import time
import os
import json
from datetime import datetime, timedelta, time as dt_time

# --- CONSTANTS ---
API_BASE = "https://apiconnect.angelbroking.com/rest"
CACHE_FILE = "angel_lite_master.json" # Saving as JSON, not Pickle

INDEX_TOKENS = {
    "NIFTY": ("NSE", "99926000"),
    "BANKNIFTY": ("NSE", "99926009"),
    "FINNIFTY": ("NSE", "99926037"),
    "SENSEX": ("BSE", "99919000"),
}

# --- MATH ---
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

def delta_only(S, K, T, r, sigma, option_type="CE"):
    if T <= 0: return 0.0
    d1, _ = d1_d2(S, K, T, r, sigma)
    if d1 is None: return 0.0
    return norm_cdf(d1) if option_type == "CE" else (norm_cdf(d1) - 1.0)

# --- STATE ---
class AppState:
    jwt_token = None
    headers = None
    master_data = [] # List of Dictionaries (No Pandas)
    logged_in = False
    auto_refresh = False
    risk_free_rate = 0.07
    sigma_default = 0.18
    strike_count = 10 

state = AppState()

# --- API HELPERS ---
def login_angel(api_key, client_code, pin, totp_secret):
    try:
        totp = pyotp.TOTP(totp_secret).now()
        url = f"{API_BASE}/auth/angelbroking/user/v1/loginByPassword"
        payload = {"clientcode": client_code, "password": pin, "totp": totp}
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-UserType": "USER",
            "X-SourceID": "WEB",
            "X-ClientLocalIP": "127.0.0.1",
            "X-ClientPublicIP": "127.0.0.1",
            "X-MACAddress": "00-00-00-00-00-00",
            "X-PrivateKey": api_key,
        }
        r = requests.post(url, json=payload, headers=headers, timeout=10)
        data = r.json()
        if data.get("status") and data.get("data", {}).get("jwtToken"):
            return data["data"]["jwtToken"]
    except Exception as e: print(e)
    return None

def get_headers(api_key, jwt_token):
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-UserType": "USER",
        "X-SourceID": "WEB",
        "X-ClientLocalIP": "127.0.0.1",
        "X-ClientPublicIP": "127.0.0.1",
        "X-MACAddress": "00-00-00-00-00-00",
        "X-PrivateKey": api_key,
        "Authorization": f"Bearer {jwt_token}",
    }

def load_token_master(log_func=None):
    # 1. CHECK CACHE
    if os.path.exists(CACHE_FILE):
        try:
            file_time = datetime.fromtimestamp(os.path.getmtime(CACHE_FILE))
            if datetime.now() - file_time < timedelta(hours=12):
                if log_func: log_func(f"Using Cached Master...")
                with open(CACHE_FILE, 'r') as f:
                    return json.load(f)
        except: pass

    try:
        if log_func: log_func("Downloading Master (Pure JSON)...")
        url = "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
        r = requests.get(url, timeout=90)
        data = r.json()
        
        # PURE PYTHON FILTERING (No Pandas)
        # 1. Filter for OPTIDX
        # 2. Clean tokens
        optimized_list = []
        for item in data:
            if item.get('instrumenttype') == 'OPTIDX':
                # Clean Token
                raw_token = str(item.get('token', '')).split('.')[0].strip()
                item['token'] = raw_token
                item['exch_seg'] = str(item.get('exch_seg', '')).upper().strip()
                # Parse Strike to Float once
                try:
                    item['strike_real'] = float(item.get('strike', 0)) / 100.0
                except:
                    item['strike_real'] = 0.0
                optimized_list.append(item)
        
        with open(CACHE_FILE, 'w') as f:
            json.dump(optimized_list, f)
            
        return optimized_list
    except Exception as e: 
        if log_func: log_func(f"Master Error: {e}")
        return []

def get_spot_price(symbol="NIFTY"):
    exch, token = INDEX_TOKENS.get(symbol, (None, None))
    if not exch: return 0.0
    url = f"{API_BASE}/secure/angelbroking/market/v1/quote/"
    payload = {"mode": "LTP", "exchangeTokens": {exch: [token]}}
    try:
        r = requests.post(url, json=payload, headers=state.headers, timeout=5)
        d = r.json()
        if d.get("status"):
            ltp = float(d['data']['fetched'][0]['ltp'])
            if symbol in ["NIFTY", "FINNIFTY"] and ltp > 100000: ltp /= 10
            elif symbol == "BANKNIFTY" and ltp > 200000: ltp /= 10
            elif symbol == "SENSEX" and ltp > 200000: ltp /= 10
            return ltp
    except: pass
    return 0.0

def get_batch_quotes(tokens, exchange="NFO", log_func=None):
    results = {}
    chunk_size = 20
    
    str_tokens = [str(t).strip() for t in tokens]
    
    for i in range(0, len(str_tokens), chunk_size):
        chunk = str_tokens[i:i + chunk_size]
        url = f"{API_BASE}/secure/angelbroking/market/v1/quote/"
        payload = {"mode": "FULL", "exchangeTokens": {exchange: chunk}}
        
        try:
            time.sleep(0.2) 
            r = requests.post(url, json=payload, headers=state.headers, timeout=5)
            d = r.json()
            
            if d.get("status") and 'fetched' in d['data']:
                for item in d['data']['fetched']:
                    # Use symbolToken to match request
                    t = item.get('symbolToken') 
                    ltp = float(item.get('ltp', 0))
                    if ltp == 0: ltp = float(item.get('close', 0))
                    if ltp > 50000: ltp /= 100.0 
                    if t: results[t] = ltp
        except Exception as e:
            pass
            
    return results

def get_expiries(symbol, log_func=None):
    if not state.master_data: return []
    
    # Pure Python Filter & Set
    exps = set()
    for item in state.master_data:
        if item.get('name') == symbol:
            exps.add(item.get('expiry'))
            
    exps_list = list(exps)
    
    try: exps_list.sort(key=lambda x: datetime.strptime(x, "%d%b%Y"))
    except: pass
    
    today = datetime.now().date()
    valid = []
    for e in exps_list:
        try:
            if datetime.strptime(e, "%d%b%Y").date() >= today: valid.append(e)
        except: pass
        
    if not valid and exps_list: return exps_list[-5:]
    return valid[:6]

def get_chain_data(symbol, expiry, spot):
    if spot == 0 or not expiry: return []
    
    # 1. Filter List (Pure Python)
    subset = []
    for item in state.master_data:
        if item.get('name') == symbol and item.get('expiry') == expiry:
            # Calculate diff
            item['diff'] = abs(item['strike_real'] - spot)
            subset.append(item)
            
    # 2. Sort by closeness to spot
    subset.sort(key=lambda x: x['diff'])
    
    # 3. Slice top N (CE + PE)
    limit = state.strike_count * 4 
    top_picks = subset[:limit]
    
    # 4. Sort by Strike for display
    top_picks.sort(key=lambda x: x['strike_real'])
    
    return top_picks

# --- MAIN UI ---
def main(page: ft.Page):
    page.title = "Angel Options"
    page.theme_mode = "dark"
    page.scroll = "adaptive"

    debug_box = ft.TextField(label="Log", height=80, text_size=10, read_only=True)
    def log(msg):
        ts = datetime.now().strftime("%H:%M:%S")
        debug_box.value = f"[{ts}] {msg}" 
        page.update()

    # -- LOGIN --
    status_txt = ft.Text("Please Login", color="red")
    progress_bar = ft.ProgressBar(width=200, color="amber", visible=False)
    api_input = ft.TextField(label="API Key", password=True, can_reveal_password=True)
    client_input = ft.TextField(label="Client Code")
    pin_input = ft.TextField(label="PIN", password=True, width=100)
    totp_input = ft.TextField(label="TOTP", password=True)

    # -- CHAIN VIEW --
    spot_lbl = ft.Text("Spot: 0.0", size=18, weight="bold")
    manual_spot = ft.TextField(label="Man. Spot", width=100, text_size=12)
    idx_dd = ft.Dropdown(options=[ft.dropdown.Option(x) for x in INDEX_TOKENS.keys()], value="NIFTY", label="Index", width=100)
    exp_dd = ft.Dropdown(label="Expiry", width=120)
    refresh_switch = ft.Switch(label="Auto", value=False)
    
    data_table = ft.DataTable(
        columns=[
            ft.DataColumn(ft.Text("LTP(C)", color="green", weight="bold")),
            ft.DataColumn(ft.Text("FairC")),
            ft.DataColumn(ft.Text("Strike", weight="bold")),
            ft.DataColumn(ft.Text("LTP(P)", color="red", weight="bold")),
            ft.DataColumn(ft.Text("FairP")),
            ft.DataColumn(ft.Text("Delta")),
        ],
        column_spacing=10,
        data_row_min_height=35,
        heading_row_height=40,
        rows=[]
    )

    # -- CALC VIEW --
    calc_res_lbl = ft.Text("Result: ", size=16)
    calc_type = ft.Dropdown(options=[ft.dropdown.Option("CE"), ft.dropdown.Option("PE")], value="CE", label="Type", width=100)
    calc_spot = ft.TextField(label="Spot", value="24000", width=150)
    calc_strike = ft.TextField(label="Strike", value="24000", width=150)
    calc_days = ft.TextField(label="Days", value="1", width=100)
    calc_vol = ft.TextField(label="Vol %", value="18", width=100)

    # -- SETTINGS --
    set_rfr = ft.TextField(label="RFR (0.07)", value=str(state.risk_free_rate))
    set_iv = ft.TextField(label="Default IV (0.18)", value=str(state.sigma_default))
    set_strikes = ft.TextField(label="Strikes Count (Default 10)", value=str(state.strike_count))

    # --- LOGIC ---
    def update_expiries(e=None):
        exps = get_expiries(idx_dd.value, log)
        exp_dd.options = [ft.dropdown.Option(x) for x in exps]
        if exps: exp_dd.value = exps[0]
        page.update()

    def refresh_chain(e):
        if not state.master_data: return
        status_txt.value = "Fetching..."
        page.update()

        spot = 0.0
        if manual_spot.value:
            try: spot = float(manual_spot.value)
            except: pass
        if spot == 0: spot = get_spot_price(idx_dd.value)
        spot_lbl.value = f"{idx_dd.value}: {spot}"
        
        if spot == 0: 
            status_txt.value = "Spot is 0"
            page.update()
            return

        if not exp_dd.value: return
        
        # 1. Get Chain (Pure Python)
        chain_list = get_chain_data(idx_dd.value, exp_dd.value, spot)
        if not chain_list: 
            status_txt.value = "No Strikes"
            page.update()
            return

        # 2. Group for Batching
        nfo_tokens = [x['token'] for x in chain_list if x['exch_seg'] == 'NFO']
        bfo_tokens = [x['token'] for x in chain_list if x['exch_seg'] == 'BFO']
        
        quotes = {}
        if nfo_tokens: quotes.update(get_batch_quotes(nfo_tokens, 'NFO', log))
        if bfo_tokens: quotes.update(get_batch_quotes(bfo_tokens, 'BFO', log))

        log(f"Got {len(quotes)} quotes")

        rows = []
        try:
            ed = datetime.strptime(exp_dd.value, "%d%b%Y")
            T = max((datetime.combine(ed.date(), dt_time(15, 30)) - datetime.now()).total_seconds() / 31536000, 0.0001)
        except: T = 0.01
        
        # Unique strikes from list
        unique_strikes = sorted(list(set(x['strike_real'] for x in chain_list)))
        step = 50
        if len(unique_strikes) > 1: step = unique_strikes[1] - unique_strikes[0]

        for K in unique_strikes:
            # Find CE and PE items in list
            ce_item = next((x for x in chain_list if x['strike_real'] == K and str(x['symbol']).endswith("CE")), None)
            pe_item = next((x for x in chain_list if x['strike_real'] == K and str(x['symbol']).endswith("PE")), None)
            
            ltp_c = 0
            ltp_p = 0
            
            if ce_item: ltp_c = quotes.get(ce_item['token'], 0)
            if pe_item: ltp_p = quotes.get(pe_item['token'], 0)
            
            fair_c = black_scholes_price(spot, K, T, state.risk_free_rate, state.sigma_default, "CE")
            fair_p = black_scholes_price(spot, K, T, state.risk_free_rate, state.sigma_default, "PE")
            delta = delta_only(spot, K, T, state.risk_free_rate, state.sigma_default, "CE")

            is_atm = abs(spot - K) < (step / 1.8)
            row_color = "#263238" if is_atm else None 
            c_col = "green" if spot > K else "white"
            p_col = "red" if spot < K else "white"

            rows.append(ft.DataRow(
                color=row_color,
                cells=[
                    ft.DataCell(ft.Text(f"{ltp_c:.2f}", color=c_col, weight="bold", size=13)),
                    ft.DataCell(ft.Text(f"{fair_c:.0f}", size=11)),
                    ft.DataCell(ft.Text(str(int(K)), weight="bold", size=13)),
                    ft.DataCell(ft.Text(f"{ltp_p:.2f}", color=p_col, weight="bold", size=13)),
                    ft.DataCell(ft.Text(f"{fair_p:.0f}", size=11)),
                    ft.DataCell(ft.Text(f"{delta:.2f}", size=11)),
                ],
            ))
            
        data_table.rows = rows
        status_txt.value = f"Updated: {datetime.now().strftime('%H:%M:%S')}"
        status_txt.color = "green"
        page.update()

    def calc_price_handler(e):
        try:
            p = black_scholes_price(float(calc_spot.value), float(calc_strike.value), float(calc_days.value)/365, state.risk_free_rate, float(calc_vol.value)/100, calc_type.value)
            d = delta_only(float(calc_spot.value), float(calc_strike.value), float(calc_days.value)/365, state.risk_free_rate, float(calc_vol.value)/100, calc_type.value)
            calc_res_lbl.value = f"Fair: {p:.2f} | Delta: {d:.4f}"
            page.update()
        except: pass

    def save_settings(e):
        try:
            state.risk_free_rate = float(set_rfr.value)
            state.sigma_default = float(set_iv.value)
            state.strike_count = int(set_strikes.value)
            status_txt.value = "Settings Saved"
            page.update()
        except: pass

    def auto_loop():
        while state.auto_refresh:
            refresh_chain(None)
            time.sleep(15)

    def toggle_auto(e):
        state.auto_refresh = refresh_switch.value
        if state.auto_refresh: threading.Thread(target=auto_loop, daemon=True).start()
    refresh_switch.on_change = toggle_auto

    # --- VIEW SWITCHING ---
    body = ft.Container()
    
    view_chain = ft.Column([
        ft.Row([idx_dd, exp_dd]),
        ft.Row([spot_lbl, manual_spot]),
        ft.Row([refresh_switch, ft.ElevatedButton("Refresh Now", on_click=refresh_chain)]),
        ft.Divider(),
        ft.Column([data_table], scroll=ft.ScrollMode.ADAPTIVE, expand=True),
        status_txt
    ], expand=True)

    view_calc = ft.Column([
        ft.Text("Calculator", size=20, weight="bold"),
        ft.Row([calc_type, calc_days, calc_vol]),
        ft.Row([calc_spot, calc_strike]),
        ft.ElevatedButton("Calculate", on_click=calc_price_handler),
        ft.Divider(),
        calc_res_lbl
    ])

    view_settings = ft.Column([
        ft.Text("Settings", size=20, weight="bold"),
        set_rfr, set_iv, set_strikes,
        ft.ElevatedButton("Save", on_click=save_settings)
    ])

    def switch_tab(e):
        btn_text = e.control.text
        if btn_text == "Chain":
            body.content = view_chain
            refresh_chain(None)
        elif btn_text == "Calc":
            body.content = view_calc
        elif btn_text == "Settings":
            body.content = view_settings
        page.update()

    nav_row = ft.Row([
        ft.ElevatedButton("Chain", on_click=switch_tab),
        ft.ElevatedButton("Calc", on_click=switch_tab),
        ft.ElevatedButton("Settings", on_click=switch_tab),
    ], alignment="center")

    def do_login(e):
        status_txt.value = "Logging in..."
        status_txt.color = "yellow"
        progress_bar.visible = True
        page.update()
        log("Logging in...")
        jwt = login_angel(api_input.value, client_input.value, pin_input.value, totp_input.value)
        if jwt:
            state.jwt_token = jwt
            state.headers = get_headers(api_input.value, jwt)
            status_txt.value = "Loading Master..."
            page.update()
            
            data = load_token_master(log)
            
            if data:
                state.master_data = data
                state.logged_in = True
                page.clean()
                body.content = view_chain
                page.add(
                    nav_row, 
                    ft.Divider(height=1), 
                    ft.Container(content=body, expand=True),
                    ft.Divider(height=1), 
                    debug_box
                )
                update_expiries()
            else:
                status_txt.value = "Master Failed"
                progress_bar.visible = False
        else:
            status_txt.value = "Login Failed"
            progress_bar.visible = False
        page.update()

    idx_dd.on_change = update_expiries
    page.add(ft.Column([
        ft.Text("Angel Options", size=30),
        api_input, client_input, ft.Row([pin_input, totp_input]),
        ft.ElevatedButton("Login", on_click=do_login),
        progress_bar, status_txt,
        ft.Divider(), debug_box
    ], alignment="center"))

ft.app(target=main)