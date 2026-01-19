from datetime import date, datetime, timedelta
from flask import Blueprint, Response, request, jsonify, abort
from sqlalchemy import or_, func
from sqlalchemy.orm import joinedload
import requests
import yfinance as yf
from openai import OpenAI
from typing import List, Dict, Any
import os
import json
from functools import lru_cache
import time
import csv
import io

from .models import (
    db,
    User,
    Portfolio,
    Aktie,
    Watchlist,
    Transaktion,
    Chatverlauf,
    ChatEntry,
    ChatTypeEnum,
    SenderEnum,
)

from werkzeug.security import generate_password_hash, check_password_hash
from flask_jwt_extended import (

    create_access_token,
    jwt_required,
    get_jwt_identity,
)

api_bp = Blueprint("api", __name__)

# ------- Simple In-Memory Caches -------

# Kursdaten-Caching (historische Marketdata)
MARKETDATA_CACHE = {}  # Key: (symbol, period, interval) -> {"expires_at": datetime, "payload": dict}

# Company-/Ticker-Info-Caching (inkl. ISIN etc.)
COMPANYINFO_CACHE = {}  # Key: symbol -> {"expires_at": datetime, "info": dict}

# Trending-Listen-Caching
TRENDING_CACHE = {}  # Key: region -> {"expires_at": datetime, "quotes": list}

NEWSAPI_KEY = "91481214bb324738b006597446270ddb"
LLM_API_KEY = "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"
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "llama-3.1-70b-versatile")

if not NEWSAPI_KEY:
    raise RuntimeError("NEWSAPI_KEY fehlt (Env-Var).")
if not LLM_API_KEY:
    raise RuntimeError("LLM_API_KEY fehlt (Env-Var).")

def _now_utc():
    return datetime.utcnow()


def get_company_info_cached(symbol: str, ttl_seconds: int = 3600):
    """
    Holt Company-Info aus Cache oder via yfinance.Ticker.
    Wird von /companyinfo, /aktie/search und /aktie/trending verwendet.
    """
    now = _now_utc()
    entry = COMPANYINFO_CACHE.get(symbol)
    if entry and entry["expires_at"] > now:
        return entry["info"]

    # Neu von yfinance holen
    ticker = yf.Ticker(symbol)

    info = {}
    if hasattr(ticker, "info") and isinstance(ticker.info, dict):
        info = ticker.info or {}
    else:
        basic = getattr(ticker, "basic_info", {}) or {}
        fast = getattr(ticker, "fast_info", {}) or {}
        info = {**basic, **fast}

    COMPANYINFO_CACHE[symbol] = {
        "info": info,
        "expires_at": now + timedelta(seconds=ttl_seconds),
    }
    return info

client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)

# ------- Helper -------

def get_json():
    if not request.is_json:
        abort(400, description="Request must be JSON")
    return request.get_json()

# ======================
#        Auth (LOGIN Register)
# ======================
@api_bp.route("/auth/register", methods=["POST"])
def register():
    data = get_json()

    required_fields = ["username", "firstname", "lastname", "password"]
    missing = [f for f in required_fields if f not in data]
    if missing:
        abort(400, description=f"Missing fields: {', '.join(missing)}")

    # Username darf nicht doppelt sein
    if User.query.filter_by(username=data["username"]).first() is not None:
        abort(400, description="Username already taken")

    user = User(
        username=data["username"],
        firstname=data["firstname"],
        lastname=data["lastname"],
    )

    user.password = generate_password_hash(data["password"])

    db.session.add(user)
    db.session.commit()

    return jsonify(user.to_dict()), 201


@api_bp.route("/auth/login", methods=["POST"])
def login():
    data = get_json()

    if "username" not in data or "password" not in data:
        abort(400, description="Username and password are required")

    user = User.query.filter_by(username=data["username"]).first()
    if user is None:
        abort(401, description="Invalid credentials")

    # Passwort prüfen
    if not check_password_hash(user.password, data["password"]):
        # oder: if not user.check_password(data["password"]):
        abort(401, description="Invalid credentials")

    # JWT generieren, Identity = user.id
    access_token = create_access_token(identity=str(user.id))

    return jsonify({
        "access_token": access_token,
        "user": user.to_dict(),
    }), 200


@api_bp.route("/auth/me", methods=["GET"])
@jwt_required()
def me():
    current_user_id = int(get_jwt_identity())
    user = User.query.get_or_404(current_user_id)
    return jsonify(user.to_dict()), 200


# ======================
#        USERS
# ======================
@api_bp.route("/users", methods=["GET", "POST"])
@jwt_required()
def users_collection():
    if request.method == "POST":
        data = get_json()
        user = User(
            username=data["username"],
            firstname=data["firstname"],
            lastname=data["lastname"],
        )
        # Passwort hashen:
        user.password = generate_password_hash(data["password"])
        # oder: user.set_password(data["password"])

        db.session.add(user)
        db.session.commit()
        return jsonify(user.to_dict()), 201

    users = User.query.all()
    return jsonify([u.to_dict() for u in users])



@api_bp.route("/users/<int:user_id>", methods=["GET", "DELETE"])
@jwt_required()
def user_detail(user_id):
    user = User.query.get_or_404(user_id)

    if request.method == "GET":
        return jsonify(user.to_dict())

    # DELETE
    db.session.delete(user)
    db.session.commit()
    return jsonify({"message": f"User {user_id} deleted"}), 200


# ======================
#      PORTFOLIOS
# ======================

@api_bp.route("/portfolios", methods=["GET", "POST"])
@jwt_required()
def portfolios_collection():
    if request.method == "POST":
        data = get_json()
        portfolio = Portfolio(
            name=data["name"],
            user_id=data["user_id"],
        )
        db.session.add(portfolio)
        db.session.commit()
        return jsonify(portfolio.to_dict()), 201

    portfolios = Portfolio.query.all()
    return jsonify([p.to_dict() for p in portfolios])

@api_bp.route("/portfolios/<int:portfolio_id>", methods=["PUT"])
@jwt_required()
def update_portfolio(portfolio_id):
    data = get_json()

    # Portfolio suchen
    portfolio = Portfolio.query.get_or_404(portfolio_id)

    # Sicherstellen, dass der eingeloggte Benutzer nur seine eigenen Portfolios ändert
    current_user_id = int(get_jwt_identity())
    if portfolio.user_id != current_user_id:
        return jsonify({"error": "Not authorized - Not your portfolio."}), 403

    if "name" in data:
        portfolio.name = data["name"]

    db.session.commit()

    return jsonify(portfolio.to_dict()), 200

@api_bp.route("/portfolios/<int:portfolio_id>", methods=["GET", "DELETE"])
@jwt_required()
def portfolio_detail(portfolio_id):
    portfolio = Portfolio.query.get_or_404(portfolio_id)

    if request.method == "GET":
        return jsonify(portfolio.to_dict())

    # DELETE
    db.session.delete(portfolio)
    db.session.commit()
    return jsonify({"message": f"Portfolio {portfolio_id} deleted"}), 200


@api_bp.route("/users/<int:user_id>/portfolios", methods=["GET"])
@jwt_required()
def portfolios_of_user(user_id):
    # 404, falls User nicht existiert
    User.query.get_or_404(user_id)

    portfolios = Portfolio.query.filter_by(user_id=user_id).all()
    return jsonify([p.to_dict() for p in portfolios])


# ======================
#        AKTIEN
# ======================

@api_bp.route("/aktien", methods=["GET", "POST"])
@jwt_required()
def aktien_collection():
    if request.method == "POST":
        data = get_json()
        aktie = Aktie(
            name=data["name"],
            isin=data["isin"],
            firma=data.get("firma", ""),
            ausschüttungsart=data.get("ausschüttungsart"),
            kategorie=data.get("kategorie"),
            land=data.get("land"),
            beschreibung=data.get("beschreibung"),
            ebitda=data.get("ebitda"),
            nettogewinn=data.get("nettogewinn"),
            umsatz=data.get("umsatz"),
            currency=data.get("currency"),
            unternehmenswert=data.get("unternehmenswert"),
        )
        db.session.add(aktie)
        db.session.commit()
        return jsonify(aktie.to_dict()), 201

    aktien = Aktie.query.all()
    return jsonify([a.to_dict() for a in aktien])

@api_bp.route("/aktien/<int:aktie_id>", methods=["PUT"])
@jwt_required()
def update_aktie(aktie_id):
    data = get_json()

    # Aktie laden oder 404
    aktie = Aktie.query.get_or_404(aktie_id)

    # Alle optionalen Felder updaten, falls vorhanden
    if "name" in data:
        aktie.name = data["name"]
    if "isin" in data:
        aktie.isin = data["isin"]
    if "firma" in data:
        aktie.firma = data["firma"]
    if "ausschüttungsart" in data:
        aktie.ausschüttungsart = data["ausschüttungsart"]
    if "kategorie" in data:
        aktie.kategorie = data["kategorie"]
    if "land" in data:
        aktie.land = data["land"]
    if "beschreibung" in data:
        aktie.beschreibung = data["beschreibung"]
    if "ebitda" in data:
        aktie.ebitda = data["ebitda"]
    if "nettogewinn" in data:
        aktie.nettogewinn = data["nettogewinn"]
    if "umsatz" in data:
        aktie.umsatz = data["umsatz"]
    if "currency" in data:
        aktie.currency = data["currency"]
    if "unternehmenswert" in data:
        aktie.unternehmenswert = data["unternehmenswert"]

    db.session.commit()

    return jsonify(aktie.to_dict()), 200

@api_bp.route("/aktien/<int:aktie_id>", methods=["GET", "DELETE"])
@jwt_required()
def aktie_detail(aktie_id):
    aktie = Aktie.query.get_or_404(aktie_id)

    if request.method == "GET":
        return jsonify(aktie.to_dict())

    # DELETE
    db.session.delete(aktie)
    db.session.commit()
    return jsonify({"message": f"Aktie {aktie_id} deleted"}), 200


# ======================
#       WATCHLIST
# ======================

@api_bp.route("/watchlist", methods=["GET", "POST"])
@jwt_required()
def watchlist_collection():
    if request.method == "POST":
        data = get_json()
        entry = Watchlist(
            user_id=data["user_id"],
            aktie_id=data["aktie_id"],
        )
        db.session.add(entry)
        db.session.commit()
        return jsonify(entry.to_dict()), 201

    entries = Watchlist.query.all()
    return jsonify([e.to_dict() for e in entries])


@api_bp.route("/watchlist/<int:entry_id>", methods=["DELETE"])
@jwt_required()
def watchlist_detail(entry_id):
    entry = Watchlist.query.get_or_404(entry_id)
    db.session.delete(entry)
    db.session.commit()
    return jsonify({"message": f"Watchlist entry {entry_id} deleted"}), 200


@api_bp.route("/watchlist/user/<int:user_id>", methods=["GET"])
@jwt_required()
def watchlist_of_user(user_id):
    # 404, falls User nicht existiert
    User.query.get_or_404(user_id)

    entries = Watchlist.query.filter_by(user_id=user_id).all()
    return jsonify([e.to_dict() for e in entries])


# ======================
#     TRANSAKTIONEN
# ======================

@api_bp.route("/transaktionen", methods=["GET", "POST"])
@jwt_required()
def transaktionen_collection():
    if request.method == "POST":
        data = get_json()

        kaufdatum = date.fromisoformat(data["kaufdatum"])

        tx = Transaktion(
            menge=data["menge"],
            kaufpreis=data["kaufpreis"],
            kaufdatum=kaufdatum,
            aktie_id=data["aktie_id"],
            portfolio_id=data["portfolio_id"],
        )
        db.session.add(tx)
        db.session.commit()
        return jsonify(tx.to_dict()), 201

    txs = Transaktion.query.all()
    return jsonify([t.to_dict() for t in txs])

@api_bp.route("/transaktionen/<int:tx_id>", methods=["PUT"])
@jwt_required()
def update_transaktion(tx_id):
    data = get_json()

    # Transaktion finden oder 404
    tx = Transaktion.query.get_or_404(tx_id)

    # Der User darf nur Transaktionen in seinen eigenen Portfolios bearbeiten
    current_user_id = int(get_jwt_identity())
    if tx.portfolio.user_id != current_user_id:
        return jsonify({"error": "Not authorized - Not your transaction."}), 403

    # Felder aktualisieren, falls im Request enthalten
    if "menge" in data:
        tx.menge = data["menge"]

    if "kaufpreis" in data:
        tx.kaufpreis = data["kaufpreis"]

    if "kaufdatum" in data:
        tx.kaufdatum = date.fromisoformat(data["kaufdatum"])

    if "aktie_id" in data:
        tx.aktie_id = data["aktie_id"]

    if "portfolio_id" in data:
        tx.portfolio_id = data["portfolio_id"]

    db.session.commit()

    return jsonify(tx.to_dict()), 200

@api_bp.route("/transaktionen/<int:tx_id>", methods=["DELETE"])
@jwt_required()
def transaktion_detail(tx_id):
    tx = Transaktion.query.get_or_404(tx_id)
    db.session.delete(tx)
    db.session.commit()
    return jsonify({"message": f"Transaktion {tx_id} deleted"}), 200


@api_bp.route("/portfolios/<int:portfolio_id>/transaktionen", methods=["GET"])
@jwt_required()
def transaktionen_of_portfolio(portfolio_id):
    # 404, falls Portfolio nicht existiert
    Portfolio.query.get_or_404(portfolio_id)

    txs = Transaktion.query.filter_by(portfolio_id=portfolio_id).all()
    return jsonify([t.to_dict() for t in txs])


# ======================
#        CHATS
# ======================

@api_bp.route("/chats", methods=["GET", "POST"])
@jwt_required()
def chats_collection():
    if request.method == "POST":
        data = get_json()
        chat_type = ChatTypeEnum(data["type"])
        chat = Chatverlauf(
            type=chat_type,
            foreign_id=data["foreign_id"],
            user_id=data["user_id"],
        )
        db.session.add(chat)
        db.session.commit()
        return jsonify(chat.to_dict()), 201

    chats = Chatverlauf.query.all()
    return jsonify([c.to_dict() for c in chats])


@api_bp.route("/chats/<int:chat_id>", methods=["DELETE"])
@jwt_required()
def chat_detail(chat_id):
    chat = Chatverlauf.query.get_or_404(chat_id)
    db.session.delete(chat)
    db.session.commit()
    return jsonify({"message": f"Chat {chat_id} deleted"}), 200


# ======================
#      CHAT ENTRIES
# ======================

@api_bp.route("/chats/<int:chat_id>/entries", methods=["GET", "POST"])
@jwt_required()
def chat_entries_collection(chat_id):
    chat = Chatverlauf.query.get_or_404(chat_id)

    if request.method == "POST":
        data = get_json()
        sender = SenderEnum(data["sender"])
        entry = ChatEntry(
            chat=chat,
            sender=sender,
            text=data["text"],
        )
        db.session.add(entry)
        db.session.commit()
        return jsonify(entry.to_dict()), 201

    entries = ChatEntry.query.filter_by(chat_id=chat_id).order_by(ChatEntry.datetime).all()
    return jsonify([e.to_dict() for e in entries])


@api_bp.route("/chats/<int:chat_id>/entries/<int:entry_id>", methods=["DELETE"])
@jwt_required()
def chat_entry_detail(chat_id, entry_id):
    # sicherstellen, dass Chat existiert
    Chatverlauf.query.get_or_404(chat_id)

    entry = ChatEntry.query.filter_by(id=entry_id, chat_id=chat_id).first()
    if entry is None:
        abort(404, description="Chat entry not found")

    db.session.delete(entry)
    db.session.commit()
    return jsonify({"message": f"Chat entry {entry_id} in chat {chat_id} deleted"}), 200


@api_bp.route("/chatbot/stock", methods=["POST"])
def chatbot_stock_assistant():
    data = get_json()
    message = (data.get("message") or "").strip()
    stock_payload = data.get("stock") or {}
    history = data.get("history") or []

    if not message:
        abort(400, description="Field 'message' is required")

    symbol = (
        stock_payload.get("symbol")
        or stock_payload.get("ticker")
        or stock_payload.get("isin")
        or "dieser Aktie"
    )
    display_name = (
        stock_payload.get("name")
        or stock_payload.get("shortName")
        or stock_payload.get("longName")
        or symbol
    )

    # Dummy response that references latest user input and current stock context
    summary_hint = "" if not history else " Ich berücksichtige den bisherigen Verlauf."
    stock_descriptor = f"{display_name} ({symbol})" if display_name != symbol else display_name
    reply = (
        f"Zu {stock_descriptor}: Ich kann dir einen allgemeinen Hinweis geben. "
        f"Deine Frage war: '{message}'.{summary_hint}"
    )

    return jsonify({
        "reply": reply,
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }), 200


# ======================
#      Market-Data
# ======================
@api_bp.route("/marketdata", methods=["GET"])
def marketdata():
    symbol = request.args.get("symbol")
    if not symbol:
        abort(400, description="Query parameter 'symbol' is required (e.g., AAPL, MSFT, BMW.DE).")

    # Default values if not provided
    period = request.args.get("range", "1mo")
    interval = request.args.get("interval", "1d")

    # -------- Caching-Logik --------
    cache_key = (symbol, period, interval)
    now = _now_utc()
    cache_entry = MARKETDATA_CACHE.get(cache_key)

    # TTL z.B. 5 Minuten
    ttl_seconds = 300

    if cache_entry and cache_entry["expires_at"] > now:
        # Direkt aus Cache antworten
        return jsonify(cache_entry["payload"]), 200

    # -------- Wenn nicht im Cache oder abgelaufen: frische Daten holen --------
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period=period, interval=interval)
    except Exception as e:
        abort(500, description=f"Error fetching market data: {str(e)}")

    if hist.empty:
        abort(404, description=f"No market data found for symbol '{symbol}'.")

    data = []
    for ts, row in hist.iterrows():
        data.append({
            "datetime": ts.isoformat(),
            "open": float(row["Open"]),
            "high": float(row["High"]),
            "low": float(row["Low"]),
            "close": float(row["Close"]),
            "volume": int(row["Volume"]) if not (row["Volume"] != row["Volume"]) else None,  # handle NaN
        })

    payload = {
        "symbol": symbol,
        "range": period,
        "interval": interval,
        "data": data,
    }

    # In Cache speichern
    MARKETDATA_CACHE[cache_key] = {
        "expires_at": now + timedelta(seconds=ttl_seconds),
        "payload": payload,
    }

    return jsonify(payload), 200

# ======================
#      Company-Info
# ======================
def fetch_company_info(symbol: str) -> Dict[str, Any]:
    """
    Zentrale Funktion zum Laden der Company-Daten via yfinance.
    Wird von mehreren Endpoints verwendet.
    """
    ticker = yf.Ticker(symbol)
    info = {}

    if hasattr(ticker, "info") and isinstance(ticker.info, dict):
        info = ticker.info or {}
    else:
        basic = getattr(ticker, "basic_info", {}) or {}
        fast = getattr(ticker, "fast_info", {}) or {}
        info = {**basic, **fast}

    return info

def compact_company_info(info: Dict[str, Any]) -> Dict[str, Any]:
    """
    Reduziert Company-Daten auf LLM-relevante Kerndaten.
    """
    keys = [
        "longName", "shortName", "symbol",
        "sector", "industry",
        "marketCap", "enterpriseValue",
        "currentPrice", "previousClose",
        "fiftyTwoWeekRange",
        "trailingPE", "forwardPE",
        "profitMargins", "operatingMargins",
        "revenueGrowth", "earningsGrowth",
        "totalRevenue", "ebitda", "freeCashflow",
        "totalDebt", "totalCash", "debtToEquity",
        "dividendYield", "dividendRate",
        "recommendationKey", "recommendationMean",
        "targetLowPrice", "targetMeanPrice", "targetHighPrice",
        "beta",
        "fullTimeEmployees",
        "longBusinessSummary",
    ]

    compact = {
        k: info.get(k)
        for k in keys
        if k in info and info.get(k) is not None
    }

    officers = info.get("companyOfficers")
    if isinstance(officers, list) and officers:
        compact["companyOfficers"] = [
            {"name": o.get("name"), "title": o.get("title")}
            for o in officers[:5]
            if isinstance(o, dict)
        ]

    return compact


@api_bp.route("/companyinfo", methods=["GET"])
def companyinfo():
    symbol = request.args.get("symbol")
    if not symbol:
        abort(400, description="Query parameter 'symbol' is required (e.g., AAPL, MSFT, BMW.DE).")

    info = fetch_company_info(symbol)

    if not info:
        abort(404, description=f"No company data found for symbol '{symbol}'.")

    return jsonify({
        "symbol": symbol,
        "company_data": info
    }), 200

@api_bp.route("/aktie/search", methods=["GET"])
def aktie_search():
    """
    Suche nach Aktien über Namen/Firma/Symbol mit yfinance.
    - 1x yf.Search(...) für die eigentliche Suche
    - danach pro gefundenem Symbol ISIN nachladen über Ticker.get_isin()/Ticker.isin
    Antwort: nur Aktien-Daten (EQUITY), angereichert um 'ticker' und 'isin'.
    """
    query = request.args.get("name") or request.args.get("q")
    if not query:
        abort(400, description="Query parameter 'name' (oder 'q') ist erforderlich, z.B. ?name=Apple")

    try:
        search = yf.Search(query, max_results=10)
        quotes = search.quotes or []
    except Exception as e:
        abort(500, description=f"Fehler bei der Aktie-Suche: {str(e)}")

    if not quotes:
        abort(404, description=f"Keine Treffer für '{query}' gefunden.")

    quotes = [q for q in quotes if (q.get("quoteType") == "EQUITY")]

    if not quotes:
        abort(404, description=f"Keine Aktien-Treffer für '{query}' gefunden.")

    isin_cache = {}
    info_cache = {}

    enriched = []
    for q in quotes:
        symbol = q.get("symbol")
        info = {}
        isin = None

        if symbol:
            # --- info cachen ---
            if symbol in info_cache:
                info = info_cache[symbol]
            else:
                try:
                    ticker_obj = yf.Ticker(symbol)
                    info = getattr(ticker_obj, "info", {}) or {}
                except Exception:
                    info = {}
                info_cache[symbol] = info

            if symbol in isin_cache:
                isin = isin_cache[symbol]
            else:
                try:
                    ticker_obj = yf.Ticker(symbol)

                    isin = getattr(ticker_obj, "isin", None)

                    if not isin:
                        isin = ticker_obj.get_isin()

                except Exception:
                    isin = None

                isin_cache[symbol] = isin

        enriched.append({
            "symbol": symbol,
            "ticker": symbol,
            "shortname": (
                info.get("shortName")
                or q.get("shortName")
                or q.get("shortname")
            ),
            "longname": (
                info.get("longName")
                or q.get("longName")
                or q.get("longname")
            ),
            "exchange": (
                info.get("exchange")
                or q.get("exchange")
            ),
            "currency": info.get("currency"),
            "sector": info.get("sector"),
            "industry": info.get("industry"),
            "quoteType": info.get("quoteType") or q.get("quoteType"),
            "regularMarketPrice": info.get("regularMarketPrice"),
            "regularMarketChangePercent": info.get("regularMarketChangePercent"),
            "isin": isin,
            "raw_search": q,
        })

    return jsonify({"query": query, "quotes": enriched}), 200

@api_bp.route("/aktie/trending", methods=["GET"])
def aktie_trending():
    """
    Liefert Trending-Aktien von Yahoo Finance.
    """

    region = request.args.get("region", "US")
    url = f"https://query1.finance.yahoo.com/v1/finance/trending/{region}"

    headers = {"User-Agent": "Mozilla/5.0"}

    # 1) Trending-Daten von Yahoo holen
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        abort(500, description=f"Fehler beim Abrufen der Trending-Aktien: {str(e)}")

    # 2) Quotes aus der Antwort extrahieren
    try:
        results = data.get("finance", {}).get("result", [])
        if not results:
            abort(404, description=f"Keine Trending-Daten für Region '{region}' gefunden.")
        quotes = results[0].get("quotes", [])
    except Exception:
        abort(500, description="Antwortformat von Yahoo Finance unerwartet.")

    if not quotes:
        abort(404, description=f"Keine Trending-Aktien für Region '{region}' gefunden.")

    isin_cache = {}
    info_cache = {}

    enriched = []
    for q in quotes:
        symbol = q.get("symbol")
        info = {}
        isin = None

        if symbol:
            # --- info cachen ---
            if symbol in info_cache:
                info = info_cache[symbol]
            else:
                try:
                    ticker_obj = yf.Ticker(symbol)
                    info = getattr(ticker_obj, "info", {}) or {}
                except Exception:
                    info = {}
                info_cache[symbol] = info

            if symbol in isin_cache:
                isin = isin_cache[symbol]
            else:
                try:
                    ticker_obj = yf.Ticker(symbol)

                    isin = getattr(ticker_obj, "isin", None)

                    if not isin:
                        isin = ticker_obj.get_isin()
                except Exception:
                    isin = None

                isin_cache[symbol] = isin

        enriched.append({
            "symbol": symbol,
            "ticker": symbol,

            "shortname": (
                info.get("shortName")
                or q.get("shortName")
                or q.get("shortname")
            ),
            "longname": (
                info.get("longName")
                or q.get("longName")
                or q.get("longname")
            ),

            "exchange": (
                info.get("exchange")
                or q.get("fullExchangeName")
                or q.get("exchange")
            ),

            "currency": info.get("currency"),
            "sector": info.get("sector"),
            "industry": info.get("industry"),
            "quoteType": info.get("quoteType") or q.get("quoteType"),
            "regularMarketPrice": info.get("regularMarketPrice"),
            "regularMarketChangePercent": info.get("regularMarketChangePercent"),

            "isin": isin,

            "raw_trending": q,
        })

    return jsonify({
        "region": region,
        "count": len(enriched),
        "results": enriched,
    }), 200

############################################
#
#               NEWS
#
############################################


# ----------------------------
# Helpers
# ----------------------------
def get_stock_price(ticker: str) -> float:
    t = yf.Ticker(ticker)
    hist = t.history(period="1d", interval="1m")
    if hist.empty:
        raise ValueError(f"Keine Kursdaten für {ticker} gefunden.")
    return float(hist.tail(1)["Close"].iloc[0])

def get_news_articles(ticker: str, limit: int = 20, days_back: int = 3, language: str = "en"):
    today = date.today()
    frm = today - timedelta(days=days_back)

    url = "https://newsapi.org/v2/everything"
    params = {
        "q": ticker,
        "from": frm.isoformat(),
        "to": today.isoformat(),
        "language": language,
        "sortBy": "publishedAt",
        "pageSize": min(limit, 100),
        "apiKey": NEWSAPI_KEY,
    }

    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    return (r.json().get("articles") or [])[:limit]
def format_news_context(articles: List[Dict[str, Any]]) -> str:
    if not articles:
        return "Keine aktuellen Nachrichten gefunden."
    lines = []
    for i, a in enumerate(articles, start=1):
        title = (a.get("title") or "").strip()
        desc = (a.get("description") or "").strip()
        source = ((a.get("source") or {}).get("name") or "").strip()
        published = (a.get("publishedAt") or "").strip()

        text = title if title else "(ohne Titel)"
        if desc:
            text += f" — {desc}"
        meta = " | ".join([x for x in [source, published] if x])
        if meta:
            text += f" ({meta})"
        lines.append(f"{i}. {text}")
    return "\n".join(lines)

def normalize_sentiment(sentiment: Dict[str, Any]) -> Dict[str, Any]:
    try:
        score = float(sentiment.get("score", 0.0))
    except Exception:
        score = 0.0

    if score <= -0.3:
        label = "negativ"
    elif score >= 0.3:
        label = "positiv"
    else:
        label = "neutral"

    sentiment["score"] = round(score, 1)
    sentiment["label"] = label
    return sentiment

def llm_json_sentiment(ticker: str, headlines: List[str]) -> Dict[str, Any]:
    joined = "\n\n---\n\n".join(headlines[:20]) if headlines else "Keine aktuellen Nachrichten."

    prompt = f"""
    Du analysierst die Marktstimmung zur Aktie {ticker} auf Basis aktueller Nachrichten.

    Nachrichten:
    {joined}

    Aufgabe:
    - Score zwischen -1.0 (sehr negativ) und +1.0 (sehr positiv)
    - label: negativ, neutral, positiv
    - kurze Begründung auf Deutsch

    Antworte ausschließlich im JSON-Format:
    {{
      "score": <zahl>,
      "label": "<negativ|neutral|positiv>",
      "begruendung": "<kurze Erklärung>"
    }}
    """

    resp = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.0,
        top_p=1.0,
        seed=1234
    )

    content = resp.choices[0].message.content.strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[1]
        content = content.rsplit("```", 1)[0].strip()

    data = json.loads(content)

    # Score stabiler machen (weniger "Flattern" in Dezimalen)
    if "score" in data:
        try:
            data["score"] = round(float(data["score"]), 1)
        except Exception:
            pass

    return data

def llm_answer_with_news_and_company(
    ticker: str,
    user_prompt: str,
    news_context: str,
    company_compact: Dict[str, Any]
) -> str:
    system = (
        "Du bist ein Assistent für Finanz- und Nachrichtenanalyse. "
        "Nutze die bereitgestellten Company-Daten und den Nachrichten-Kontext. "
        "Wenn Infos fehlen: sag das klar. Keine Anlageberatung im Sinne einer Garantie."
    )

    company_json = json.dumps(company_compact, ensure_ascii=False)

    prompt = f"""Ticker: {ticker}

    Company-Daten (kompakt, JSON):
    {company_json}

    Nachrichten-Kontext:
    {news_context}

    User-Frage:
    {user_prompt}

    Antworte auf Deutsch.
    - Wenn du eine Prognose machst, nenne Unsicherheiten.
    - Wenn die Datenlage nicht reicht, sag: "Nicht genug Informationen".
    """
    resp = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        temperature=0.3,
    )
    return resp.choices[0].message.content.strip()

@api_bp.route("/ai/ask", methods=["GET", "POST"])
@jwt_required()
def ask():
    body = request.get_json(silent=True) or {}
    ticker = (body.get("ticker") or "").strip().upper()
    user_prompt = (body.get("prompt") or "").strip()

    if not ticker or not user_prompt:
        return jsonify({"error": "Bitte JSON senden: {\"ticker\":\"AAPL\",\"prompt\":\"...\"}"}), 400

    try:
        articles = get_news_articles(ticker, limit=20, days_back=14, language="en")
        news_context = format_news_context(articles)
        raw_company = fetch_company_info(ticker)
        company_compact = compact_company_info(raw_company)

        answer = llm_answer_with_news_and_company(ticker, user_prompt, news_context, company_compact)

        return jsonify({
            "ticker": ticker,
            "used_articles": len(articles),
            "answer": answer,
        })

    except RuntimeError as e:
        return jsonify({"error": "Upstream error", "detail": e.args[0]}), 502
    except Exception as e:
        return jsonify({"error": "Internal error", "detail": str(e)}), 500

@api_bp.route("/ai/sentiment", methods=["GET", "POST"])
@jwt_required()
def news_sentiment():
    body = request.get_json(silent=True) or {}

    ticker = (body.get("ticker") or "").strip().upper()
    if not ticker:
        return jsonify({"error": "Bitte JSON senden: {\"ticker\":\"AAPL\"}"}), 400

    try:
        articles = get_news_articles(
            ticker=ticker,
            limit=10,
            days_back=3,
            language="en"
        )

        # Stabilisieren: sortieren + Duplikate entfernen
        articles = sorted(
            articles,
            key=lambda a: ((a.get("publishedAt") or ""), (a.get("url") or "")),
            reverse=True
        )

        seen = set()
        unique = []
        for a in articles:
            url = (a.get("url") or "").strip()
            if url and url in seen:
                continue
            if url:
                seen.add(url)
            unique.append(a)
        articles = unique

        news_context = format_news_context(articles)

        sentiment = llm_json_sentiment(ticker, [news_context])
        sentiment = normalize_sentiment(sentiment)
        
        return jsonify({
            "ticker": ticker,
            "used_articles": len(articles),
            "sentiment": sentiment
        })

    except RuntimeError as e:
        return jsonify({"error": "Upstream error", "detail": e.args[0]}), 502
    except Exception as e:
        return jsonify({"error": "Internal error", "detail": str(e)}), 500

def llm_answer_with_portfolio_context(portfolio: dict, user_prompt: str) -> str:
    system = (
        "Du bist ein Assistent für Portfolio- und Aktienanalyse. "
        "Nutze den bereitgestellten Portfolio-Kontext (Positionen) um die Frage zu beantworten. "
        "Wenn Informationen fehlen, sage das klar. Keine Anlageberatung im Sinne einer Garantie."
    )

    prompt = f"""
            Portfolio-Kontext (JSON):
            {json.dumps(portfolio, ensure_ascii=False)}

            User-Frage:
            {user_prompt}

            Antworte auf Deutsch.
            - Wenn du Annahmen triffst, nenne sie.
            - Wenn die Datenlage nicht reicht, sag: "Nicht genug Informationen".
            """

    resp = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        temperature=0.3,
    )
    return resp.choices[0].message.content.strip()


@api_bp.route("/ai/portfolio/ask", methods=["POST"])
@jwt_required()
def ask_portfolio():
    body = request.get_json(silent=True) or {}
    portfolio_id = body.get("portfolio_id")
    user_prompt = (body.get("prompt") or "").strip()

    if not portfolio_id or not user_prompt:
        return jsonify({"error": "Bitte JSON senden: {\"portfolio_id\": 1, \"prompt\": \"...\"}"}), 400

    user_id = get_jwt_identity()

    try:
        # Portfolio laden + Ownership prüfen
        portfolio_obj = Portfolio.query.filter_by(id=portfolio_id, user_id=user_id).first()
        if not portfolio_obj:
            return jsonify({"error": "Portfolio nicht gefunden oder kein Zugriff."}), 404

        # Positionen aggregieren: Summe(menge) je Aktie
        rows = (
            db.session.query(
                Aktie.id.label("aktie_id"),
                Aktie.isin.label("isin"),
                Aktie.name.label("name"),
                Aktie.firma.label("firma"),
                func.coalesce(func.sum(Transaktion.menge), 0).label("menge_summe"),
            )
            .join(Transaktion, Transaktion.aktie_id == Aktie.id)
            .filter(Transaktion.portfolio_id == portfolio_id)
            .group_by(Aktie.id, Aktie.isin, Aktie.name, Aktie.firma)
            .all()
        )

        positions = []
        for r in rows:
            positions.append({
                "aktie_id": int(r.aktie_id),
                "isin": r.isin,
                "name": r.name,
                "firma": r.firma,
                "menge": float(r.menge_summe),
            })

        portfolio_context = {
            "portfolio_id": int(portfolio_obj.id),
            "portfolio_name": portfolio_obj.name,
            "positions": positions,
            "hinweis": "ISIN statt Ticker, da im Datenmodell kein Ticker-Feld existiert."
        }

        answer = llm_answer_with_portfolio_context(portfolio_context, user_prompt)

        return jsonify({
            "portfolio_id": int(portfolio_obj.id),
            "positions_count": len(positions),
            "answer": answer,
            "context": portfolio_context
        })

    except RuntimeError as e:
        return jsonify({"error": "Upstream error", "detail": e.args[0]}), 502
    except Exception as e:
        return jsonify({"error": "Internal error", "detail": str(e)}), 500

#   PORTFOLIO PERFORMANCE

# ISIN -> Yahoo Symbol Cache (In-Memory)
# Key: isin -> {"expires_at": datetime, "symbol": str}
ISIN_SYMBOL_CACHE = {}

def resolve_symbol_from_isin(isin: str, ttl_seconds: int = 24 * 3600):
    """
    ISIN -> Yahoo Finance Symbol via yfinance.Search(isin).
    Ergebnis wird gecacht.
    """
    if not isin:
        return None

    now = _now_utc()
    entry = ISIN_SYMBOL_CACHE.get(isin)
    if entry and entry.get("expires_at") and entry["expires_at"] > now:
        return entry.get("symbol")

    symbol = None
    try:
        s = yf.Search(isin, max_results=10)
        quotes = s.quotes or []

        # Bevorzugt EQUITY
        equities = [
            q for q in quotes
            if (q.get("quoteType") == "EQUITY" and q.get("symbol"))
        ]
        if equities:
            symbol = equities[0].get("symbol")
        else:
            # Fallback: erster Treffer mit Symbol
            for q in quotes:
                if q.get("symbol"):
                    symbol = q.get("symbol")
                    break
    except Exception:
        symbol = None

    ISIN_SYMBOL_CACHE[isin] = {
        "symbol": symbol,
        "expires_at": now + timedelta(seconds=ttl_seconds),
    }
    return symbol

def get_price_and_prevclose(symbol: str):
    """
    Holt aktuellen Kurs und previousClose über yfinance.
    """
    try:
        t = yf.Ticker(symbol)

        info = getattr(t, "info", {}) or {}
        price = info.get("regularMarketPrice") or info.get("currentPrice")
        prev = info.get("previousClose")

        # Fallback: fast_info
        if (price is None) or (prev is None):
            fast = getattr(t, "fast_info", {}) or {}
            if price is None:
                price = fast.get("last_price") or fast.get("lastPrice")
            if prev is None:
                prev = fast.get("previous_close") or fast.get("previousClose")

        price = float(price) if price is not None else None
        prev = float(prev) if prev is not None else None
        return price, prev

    except Exception:
        return None, None

@api_bp.route("/portfolios/<int:portfolio_id>/performance", methods=["GET"])
@jwt_required()
def portfolio_performance(portfolio_id):
    """
    Ticket KPIs:
    - Absolute Rendite (€)
    - Absolute Rendite (%)
    - Tagesveränderung (%)
    - Gesamtwert des Portfolios

    Annahmen:
    - kaufpreis ist in EUR (oder bereits in der Zielwährung).
    - Aktien sind über ISIN im DB-Modell gespeichert -> ISIN wird auf Yahoo Symbol gemappt.
    """

    portfolio = Portfolio.query.get_or_404(portfolio_id)

    # Ownership prüfen (JWT identity ist bei dir string -> in int casten)
    current_user_id = int(get_jwt_identity())
    if portfolio.user_id != current_user_id:
        return jsonify({"error": "Not authorized - Not your portfolio."}), 403

    # Aggregation:
    # qty = Summe(menge) je Aktie
    # invested = Summe(menge * kaufpreis) je Aktie
    rows = (
        db.session.query(
            Aktie.id.label("aktie_id"),
            Aktie.isin.label("isin"),
            func.coalesce(func.sum(Transaktion.menge), 0).label("menge_summe"),
            func.coalesce(func.sum(Transaktion.menge * Transaktion.kaufpreis), 0).label("invest_summe"),
        )
        .join(Transaktion, Transaktion.aktie_id == Aktie.id)
        .filter(Transaktion.portfolio_id == portfolio_id)
        .group_by(Aktie.id, Aktie.isin)
        .all()
    )

    total_value = 0.0
    total_prev_value = 0.0
    total_invested = 0.0

    # optional: pro Position Details (kannst du später entfernen)
    positions_debug = []

    for r in rows:
        qty = float(r.menge_summe or 0.0)
        invested = float(r.invest_summe or 0.0)

        # Positionen mit 0 ignorieren
        if qty == 0:
            continue

        isin = r.isin
        symbol = resolve_symbol_from_isin(isin)

        if not symbol:
            positions_debug.append({
                "aktie_id": int(r.aktie_id),
                "isin": isin,
                "symbol": None,
                "qty": qty,
                "error": "no_symbol_for_isin",
            })
            continue

        price, prev_close = get_price_and_prevclose(symbol)

        if price is None:
            positions_debug.append({
                "aktie_id": int(r.aktie_id),
                "isin": isin,
                "symbol": symbol,
                "qty": qty,
                "error": "no_price",
            })
            continue

        pos_value = qty * price
        total_value += pos_value

        if prev_close is not None and prev_close != 0:
            total_prev_value += qty * prev_close

        total_invested += invested

        positions_debug.append({
            "aktie_id": int(r.aktie_id),
            "isin": isin,
            "symbol": symbol,
            "qty": qty,
            "price": price,
            "prev_close": prev_close,
            "pos_value": pos_value,
            "invested": invested,
        })

    abs_return_eur = total_value - total_invested

    abs_return_pct = None
    if total_invested != 0:
        abs_return_pct = (abs_return_eur / total_invested) * 100.0

    daily_change_pct = None
    if total_prev_value != 0:
        daily_change_pct = ((total_value - total_prev_value) / total_prev_value) * 100.0

    def r2(x):
        return round(float(x), 2)

    return jsonify({
        "portfolio_id": portfolio_id,

        # Ticket KPI Felder:
        "total_value": r2(total_value),
        "absolute_return_eur": r2(abs_return_eur),
        "absolute_return_pct": r2(abs_return_pct) if abs_return_pct is not None else None,
        "daily_change_pct": r2(daily_change_pct) if daily_change_pct is not None else None,

        "currency": "USD",

        # optional für Debug/QA:
        "positions": positions_debug,
        "meta": {
            "invested_total": r2(total_invested),
            "prev_total_value": r2(total_prev_value),
            "note": "ISIN->Symbol via yfinance.Search(); Preise via yfinance info/fast_info."
        }
    }), 200


@api_bp.route("/screener", methods=["GET"])
def aktie_screener():
    screener_type = request.args.get("type", "most_actives")
    region = request.args.get("region", "US")
    count = min(int(request.args.get("count", 25)), 25)

    PREDEFINED_SCREENERS = {
        "most_actives": "Most Active",
        "day_gainers": "Top Gainers",
        "day_losers": "Top Losers",
        "growth_technology_stocks": "Growth Technology",
        "undervalued_growth_stocks": "Undervalued Growth",
        "esg_leaders": "ESG Leaders",
        "green_energy_stocks": "Green Energy",
        "artificial_intelligence": "AI Stocks",
    }

    if screener_type not in PREDEFINED_SCREENERS:
        abort(400, "Unbekannter Screener")

    # 5-Minuten-TTL
    cache_bucket = int(time.time() / 300)

    data = fetch_screener_cached(
        screener_type,
        region,
        count,
        cache_bucket
    )

    if not data:
        return jsonify({
            "screener": screener_type,
            "label": PREDEFINED_SCREENERS[screener_type],
            "region": region,
            "count": 0,
            "warning": "Yahoo Screener aktuell nicht verfügbar",
            "results": []
        }), 200

    result = data.get("finance", {}).get("result", [])
    quotes = result[0].get("quotes", []) if result else []

    results = [{
        "symbol": q.get("symbol"),
        "shortname": q.get("shortName"),
        "exchange": q.get("fullExchangeName") or q.get("exchange"),
        "regularMarketPrice": q.get("regularMarketPrice"),
        "regularMarketChangePercent": q.get("regularMarketChangePercent"),
        "marketCap": q.get("marketCap"),
    } for q in quotes]

    return jsonify({
        "screener": screener_type,
        "label": PREDEFINED_SCREENERS[screener_type],
        "region": region,
        "count": len(results),
        "results": results,
    }), 200


@lru_cache(maxsize=128)
def fetch_screener_cached(screener_type, region, count, cache_bucket):
    url = "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved"

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json"
    }

    params = {
        "scrIds": screener_type,
        "count": count,
        "region": region,
    }

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        data = resp.json()

        finance = data.get("finance", {})
        if finance.get("error"):
            return None

        return data
    except Exception:
        return None

#   AI: KI Aktiensuche
# zuerst Hilfsfunktionen und unten die API Schnittstelle.:

def _safe_str(x):
    return (str(x).strip() if x is not None else "")

def _parse_llm_json(content: str) -> dict:
    if not content:
        return {}
    c = content.strip()
    if c.startswith("```"):
        c = c.split("\n", 1)[1]
        if "```" in c:
            c = c.rsplit("```", 1)[0].strip()
    return json.loads(c)

def _get_isin_for_symbol(symbol: str):
    """
    holt ISIN aus yfinance
    """
    symbol = _safe_str(symbol).upper()
    if not symbol:
        return None
    try:
        t = yf.Ticker(symbol)
        isin = getattr(t, "isin", None)
        if not isin:
            try:
                isin = t.get_isin()
            except Exception:
                isin = None
        return isin
    except Exception:
        return None

def _build_exclusion_sets(existing_stocks: list):
    isins = set()
    symbols = set()
    aktie_ids = set()

    if not isinstance(existing_stocks, list):
        return isins, symbols, aktie_ids

    for item in existing_stocks:
        if isinstance(item, dict):
            isin = _safe_str(item.get("isin"))
            sym = _safe_str(item.get("symbol") or item.get("ticker"))
            aid = item.get("aktie_id") or item.get("id")

            if isin:
                isins.add(isin.upper())
            if sym:
                symbols.add(sym.upper())
            if aid is not None:
                try:
                    aktie_ids.add(int(aid))
                except Exception:
                    pass

        elif isinstance(item, (int, float)) and str(item).isdigit():
            try:
                aktie_ids.add(int(item))
            except Exception:
                pass
        else:
            s = _safe_str(item)
            if not s:
                continue
            if len(s) == 12 and s[:2].isalpha():
                isins.add(s.upper())
            else:
                symbols.add(s.upper())

    return isins, symbols, aktie_ids

def _load_portfolio_existing_stocks(portfolio_id: int, current_user_id: int):
    """
    falls die portfolio_id übergeben wird, hole vorhandene Aktie-IDs + ISINs aus DB.
    """
    portfolio_obj = Portfolio.query.filter_by(id=portfolio_id, user_id=current_user_id).first()
    if not portfolio_obj:
        return set(), set(), set(), "Portfolio nicht gefunden oder kein Zugriff."

    rows = (
        db.session.query(
            Aktie.id.label("aktie_id"),
            Aktie.isin.label("isin"),
        )
        .join(Transaktion, Transaktion.aktie_id == Aktie.id)
        .filter(Transaktion.portfolio_id == portfolio_id)
        .group_by(Aktie.id, Aktie.isin)
        .all()
    )

    isins = set()
    aktie_ids = set()
    for r in rows:
        if r.isin:
            isins.add(str(r.isin).upper())
        try:
            aktie_ids.add(int(r.aktie_id))
        except Exception:
            pass

    return isins, set(), aktie_ids, None

def llm_extract_filters_and_candidates(user_prompt: str, exclude_isins: list, exclude_symbols: list, limit: int = 12) -> dict:
    system = (
        "Du bist ein Aktien-Screener-Assistent. "
        "Du musst den Userprompt als Filter-Set interpretieren und Kandidaten vorschlagen. "
        "WICHTIG: Der Prompt ist als FILTER zu verstehen (nicht ignorieren). "
        "WICHTIG: Bereits vorhandene Aktien (Exclude-Liste) dürfen NICHT vorgeschlagen werden. "
        "Antworte ausschließlich als gültiges JSON ohne Markdown."
    )

    prompt = f"""
Userprompt:
{user_prompt}

Exclude ISINs:
{json.dumps(exclude_isins, ensure_ascii=False)}

Exclude Symbols:
{json.dumps(exclude_symbols, ensure_ascii=False)}

Aufgabe:
1) Extrahiere Filter aus dem Prompt (z.B. Region/Land, Branche/Sektor, Dividende ja/nein, Wachstum/Value, Risiko/Volatilität, MarketCap grob, Währung).
2) Schlage {limit} börsennotierte Aktien (Ticker/Symbol) vor, die zu den Filtern passen.
3) Vermeide die Excludes strikt.
4) Gib pro Vorschlag einen kurzen Grund (1-2 Sätze) und ein match_score von 0 bis 100.

JSON Schema (genau so, keine zusätzlichen Felder oben drüber):
{{
  "filters": {{
    "region": "<optional>",
    "countries": ["<optional>"],
    "sectors": ["<optional>"],
    "themes": ["<optional>"],
    "dividend_focus": <true|false|null>,
    "style": "<value|growth|quality|income|blend|null>",
    "risk": "<low|medium|high|null>",
    "market_cap": "<small|mid|large|null>",
    "currency": "<optional>"
  }},
  "candidates": [
    {{
      "symbol": "AAPL",
      "name_hint": "<optional>",
      "match_score": 0,
      "reason": "..."
    }}
  ]
}}
"""

    resp = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
        top_p=1.0,
    )
    return _parse_llm_json(resp.choices[0].message.content.strip())

def _enrich_and_filter_candidates(candidates: list, exclude_isins: set, exclude_symbols: set, limit: int = 12):
    results = []
    seen_symbols = set()

    for c in (candidates or []):
        if len(results) >= limit:
            break

        if isinstance(c, str):
            symbol = _safe_str(c).upper()
            match_score = None
            reason = None
        elif isinstance(c, dict):
            symbol = _safe_str(c.get("symbol")).upper()
            match_score = c.get("match_score")
            reason = _safe_str(c.get("reason"))
        else:
            continue

        if not symbol:
            continue
        if symbol in exclude_symbols:
            continue
        if symbol in seen_symbols:
            continue

        info = get_company_info_cached(symbol, ttl_seconds=3600)
        if not isinstance(info, dict) or not info:
            continue

        isin = info.get("isin") or _get_isin_for_symbol(symbol)
        if isin:
            isin = str(isin).upper()

        if isin and isin in exclude_isins:
            continue

        enriched = {
            "symbol": symbol,
            "isin": isin,
            "name": info.get("longName") or info.get("shortName") or info.get("displayName"),
            "sector": info.get("sector"),
            "industry": info.get("industry"),
            "country": info.get("country"),
            "currency": info.get("currency"),
            "marketCap": info.get("marketCap"),
            "currentPrice": info.get("regularMarketPrice") or info.get("currentPrice"),
            "previousClose": info.get("previousClose"),
            "dividendYield": info.get("dividendYield"),
            "beta": info.get("beta"),
            "match_score": match_score,
            "reason": reason,
        }

        seen_symbols.add(symbol)
        results.append(enriched)

    return results

@api_bp.route("/ai/stock-search", methods=["POST"])
@jwt_required()
def ai_stock_search():
    """
    Ticket #61: KI unterstützte Aktiensuche

    Input JSON:
    {
      "prompt": "Suche defensive Dividenden-Aktien aus Europa ...",
      "existing_stocks": ["US0378331005","AAPL", {"aktie_id": 1}, ...],   # optional
      "portfolio_id": 2,  # optional (wenn gesetzt, wird zusätzlich aus DB excluded)
      "limit": 12
    }

    Output:
    {
      "filters": {...},
      "excluded": {...},
      "results": [...]
    }
    """
    body = request.get_json(silent=True) or {}
    user_prompt = _safe_str(body.get("prompt"))
    existing_stocks = body.get("existing_stocks") or []
    portfolio_id = body.get("portfolio_id")
    limit = body.get("limit") or 12

    try:
        limit = int(limit)
    except Exception:
        limit = 12
    limit = max(1, min(limit, 25))

    if not user_prompt:
        return jsonify({"error": "Bitte JSON senden: {\"prompt\":\"...\", \"existing_stocks\":[...]}"}), 400

    current_user_id = int(get_jwt_identity())

    # Excludes aus Request
    excl_isins, excl_symbols, excl_aktie_ids = _build_exclusion_sets(existing_stocks)

    # Optional: Excludes aus Portfolio laden
    portfolio_note = None
    if portfolio_id is not None:
        try:
            pid = int(portfolio_id)
            p_isins, p_symbols, p_ids, err = _load_portfolio_existing_stocks(pid, current_user_id)
            if err:
                portfolio_note = err
            else:
                excl_isins |= p_isins
                excl_symbols |= p_symbols
                excl_aktie_ids |= p_ids
        except Exception:
            portfolio_note = "portfolio_id konnte nicht verarbeitet werden."

    # Zusätzlich: wenn aktie_ids dabei sind -> ISINs nachladen (damit Exclusion auch bei anderen Symbolen greift)
    if excl_aktie_ids:
        rows = Aktie.query.filter(Aktie.id.in_(list(excl_aktie_ids))).all()
        for a in rows:
            if a.isin:
                excl_isins.add(str(a.isin).upper())

    try:
        llm_payload = llm_extract_filters_and_candidates(
            user_prompt=user_prompt,
            exclude_isins=sorted(list(excl_isins)),
            exclude_symbols=sorted(list(excl_symbols)),
            limit=max(limit, 12),  # LLM darf ruhig mehr vorschlagen
        )

        filters = llm_payload.get("filters") or {}
        candidates = llm_payload.get("candidates") or []

        results = _enrich_and_filter_candidates(
            candidates=candidates,
            exclude_isins=excl_isins,
            exclude_symbols=excl_symbols,
            limit=limit
        )

        return jsonify({
            "prompt": user_prompt,
            "filters": filters,
            "excluded": {
                "isins": sorted(list(excl_isins))[:200],
                "symbols": sorted(list(excl_symbols))[:200],
                "aktie_ids": sorted(list(excl_aktie_ids))[:200],
                "note": portfolio_note
            },
            "counts": {
                "candidates_from_llm": len(candidates),
                "results_returned": len(results),
            },
            "results": results,
            "meta": {
                "model": LLM_MODEL,
                "note": "LLM filtert/empfiehlt, yfinance verifiziert und enrich't; Excludes werden serverseitig hart angewandt."
            }
        }), 200

    except json.JSONDecodeError as e:
        return jsonify({
            "error": "LLM hat ungültiges JSON geliefert",
            "detail": str(e)
        }), 502
    except RuntimeError as e:
        return jsonify({"error": "Upstream error", "detail": e.args[0]}), 502
    except Exception as e:
        return jsonify({"error": "Internal error", "detail": str(e)}), 500

#   Aggregierte Portfolio-Kennzahlen

def _r2(x):
    return round(float(x), 2)

def _compute_portfolio_kpis(portfolio_id: int, current_user_id: int, include_positions: bool = False) -> dict:
    """
    Berechnet dieselben KPIs wie /portfolios/<id>/performance
    und gibt ein Dict zurück (optional mit positions_debug).
    """
    portfolio = Portfolio.query.get_or_404(portfolio_id)

    # Ownership prüfen
    if portfolio.user_id != current_user_id:
        return {"error": "Not authorized - Not your portfolio.", "status": 403}

    rows = (
        db.session.query(
            Aktie.id.label("aktie_id"),
            Aktie.isin.label("isin"),
            func.coalesce(func.sum(Transaktion.menge), 0).label("menge_summe"),
            func.coalesce(func.sum(Transaktion.menge * Transaktion.kaufpreis), 0).label("invest_summe"),
        )
        .join(Transaktion, Transaktion.aktie_id == Aktie.id)
        .filter(Transaktion.portfolio_id == portfolio_id)
        .group_by(Aktie.id, Aktie.isin)
        .all()
    )

    total_value = 0.0
    total_prev_value = 0.0
    total_invested = 0.0

    positions_debug = []

    mapped_positions = 0
    missing_symbol = 0
    missing_price = 0

    for r in rows:
        qty = float(r.menge_summe or 0.0)
        invested = float(r.invest_summe or 0.0)

        if qty == 0:
            continue

        isin = r.isin
        symbol = resolve_symbol_from_isin(isin)

        if not symbol:
            missing_symbol += 1
            if include_positions:
                positions_debug.append({
                    "aktie_id": int(r.aktie_id),
                    "isin": isin,
                    "symbol": None,
                    "qty": qty,
                    "error": "no_symbol_for_isin",
                })
            continue

        price, prev_close = get_price_and_prevclose(symbol)
        if price is None:
            missing_price += 1
            if include_positions:
                positions_debug.append({
                    "aktie_id": int(r.aktie_id),
                    "isin": isin,
                    "symbol": symbol,
                    "qty": qty,
                    "error": "no_price",
                })
            continue

        mapped_positions += 1

        pos_value = qty * price
        total_value += pos_value

        if prev_close is not None and prev_close != 0:
            total_prev_value += qty * prev_close

        total_invested += invested

        if include_positions:
            positions_debug.append({
                "aktie_id": int(r.aktie_id),
                "isin": isin,
                "symbol": symbol,
                "qty": qty,
                "price": price,
                "prev_close": prev_close,
                "pos_value": pos_value,
                "invested": invested,
            })

    abs_return_eur = total_value - total_invested

    abs_return_pct = None
    if total_invested != 0:
        abs_return_pct = (abs_return_eur / total_invested) * 100.0

    daily_change_pct = None
    if total_prev_value != 0:
        daily_change_pct = ((total_value - total_prev_value) / total_prev_value) * 100.0

    payload = {
        "portfolio_id": int(portfolio.id),
        "portfolio_name": portfolio.name,
        "total_value": _r2(total_value),
        "absolute_return_eur": _r2(abs_return_eur),
        "absolute_return_pct": _r2(abs_return_pct) if abs_return_pct is not None else None,
        "daily_change_pct": _r2(daily_change_pct) if daily_change_pct is not None else None,
        "currency": "USD",
        "meta": {
            "invested_total": _r2(total_invested),
            "prev_total_value": _r2(total_prev_value),
            "mapped_positions": mapped_positions,
            "missing_symbol": missing_symbol,
            "missing_price": missing_price,
            "note": "KPIs analog zu /portfolios/<id>/performance (ISIN->Symbol via yfinance.Search(); Preise via info/fast_info).",
        }
    }

    if include_positions:
        payload["positions"] = positions_debug

    return payload


@api_bp.route("/users/<int:user_id>/portfolios/performance", methods=["GET"])
@jwt_required()
def aggregated_portfolio_performance(user_id: int):
    """
    Ticket #44: Aggregierte Portfolio-Kennzahlen
    Aggregiert die KPIs (total_value, absolute_return, daily_change) über alle Portfolios eines Users.

    Query Params:
    - include_portfolios=1  -> liefert zusätzlich pro-Portfolio KPI-Liste (default: 1)
    - include_positions=1   -> liefert positions debug je Portfolio (default: 0)
    """

    current_user_id = int(get_jwt_identity())
    if user_id != current_user_id:
        return jsonify({"error": "Not authorized - Not your user."}), 403

    include_portfolios = request.args.get("include_portfolios", "1") in ("1", "true", "True", "yes")
    include_positions = request.args.get("include_positions", "0") in ("1", "true", "True", "yes")

    # User existiert?
    User.query.get_or_404(user_id)

    portfolios = Portfolio.query.filter_by(user_id=user_id).all()

    agg_total_value = 0.0
    agg_total_prev_value = 0.0
    agg_total_invested = 0.0

    per_portfolio = []

    # Meta counters
    total_mapped_positions = 0
    total_missing_symbol = 0
    total_missing_price = 0

    for p in portfolios:
        kpis = _compute_portfolio_kpis(
            portfolio_id=int(p.id),
            current_user_id=current_user_id,
            include_positions=include_positions
        )

        # Ownership error (sollte nicht passieren, aber sicher ist sicher)
        if "error" in kpis:
            continue

        # Aggregation: Basiswerte aus meta (invested_total, prev_total_value)
        invested_total = float((kpis.get("meta") or {}).get("invested_total") or 0.0)
        prev_total_value = float((kpis.get("meta") or {}).get("prev_total_value") or 0.0)

        agg_total_value += float(kpis.get("total_value") or 0.0)
        agg_total_invested += invested_total
        agg_total_prev_value += prev_total_value

        meta = kpis.get("meta") or {}
        total_mapped_positions += int(meta.get("mapped_positions") or 0)
        total_missing_symbol += int(meta.get("missing_symbol") or 0)
        total_missing_price += int(meta.get("missing_price") or 0)

        if include_portfolios:
            per_portfolio.append(kpis)

    agg_abs_return_eur = agg_total_value - agg_total_invested

    agg_abs_return_pct = None
    if agg_total_invested != 0:
        agg_abs_return_pct = (agg_abs_return_eur / agg_total_invested) * 100.0

    agg_daily_change_pct = None
    if agg_total_prev_value != 0:
        agg_daily_change_pct = ((agg_total_value - agg_total_prev_value) / agg_total_prev_value) * 100.0

    return jsonify({
        "user_id": user_id,
        "portfolios_count": len(portfolios),

        "total_value": _r2(agg_total_value),
        "absolute_return_eur": _r2(agg_abs_return_eur),
        "absolute_return_pct": _r2(agg_abs_return_pct) if agg_abs_return_pct is not None else None,
        "daily_change_pct": _r2(agg_daily_change_pct) if agg_daily_change_pct is not None else None,

        "currency": "USD",

        "portfolios": per_portfolio if include_portfolios else None,

        "meta": {
            "invested_total": _r2(agg_total_invested),
            "prev_total_value": _r2(agg_total_prev_value),
            "mapped_positions": total_mapped_positions,
            "missing_symbol": total_missing_symbol,
            "missing_price": total_missing_price,
            "note": "Aggregation über alle Portfolios",
        }
    }), 200

@api_bp.route("/portfolios/<int:portfolio_id>/export/csv", methods=["GET"])
@jwt_required()
def export_portfolio_csv(portfolio_id):
    portfolio = (
        db.session.query(Portfolio)
        .options(
            joinedload(Portfolio.transactions)
            .joinedload(Transaktion.aktie)
        )
        .filter_by(id=portfolio_id)
        .first()
    )

    if not portfolio:
        abort(404, description="Portfolio nicht gefunden")

    output = io.StringIO()
    writer = csv.writer(output, delimiter=";")

    writer.writerow([
        "Portfolio ID",
        "Portfolio Name",
        "Aktienname",
        "ISIN",
        "Firma",
        "Menge",
        "Kaufpreis",
        "Kaufdatum",
    ])

    for tx in portfolio.transactions:
        writer.writerow([
            portfolio.id,
            portfolio.name,
            tx.aktie.name,
            tx.aktie.isin,
            tx.aktie.firma,
            float(tx.menge),
            float(tx.kaufpreis),
            tx.kaufdatum.isoformat(),
        ])

    csv_data = output.getvalue()
    output.close()

    return Response(
        csv_data,
        mimetype="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename=portfolio_{portfolio.id}.csv"
        },
    )

@api_bp.route("/portfolios/import/csv", methods=["POST"])
@jwt_required()
def import_portfolio_csv_create_if_missing():
    current_user_id = get_jwt_identity()

    if "file" not in request.files:
        abort(400, description="Keine Datei übergeben")

    file = request.files["file"]
    try:
        # UTF-8 für CSV aus Excel
        stream = io.StringIO(file.stream.read().decode("utf-8-sig"))
    except Exception as e:
        abort(400, description=f"Datei konnte nicht gelesen werden: {str(e)}")

    reader = csv.DictReader(stream, delimiter=";")
    all_rows = list(reader)
    
    if not all_rows:
        return jsonify({"message": "CSV Datei ist leer"}), 200

    # Portfolio-Logik
    portfolio_name_from_csv = all_rows[0].get("Portfolio Name", "Importiertes Portfolio")
    portfolio = Portfolio.query.filter_by(name=portfolio_name_from_csv, user_id=current_user_id).first()

    if not portfolio:
        portfolio = Portfolio(name=portfolio_name_from_csv, user_id=current_user_id)
        db.session.add(portfolio)
        db.session.flush()

    created = 0
    skipped = 0
    error_details = []

    for index, row in enumerate(all_rows):
        isin = row.get("ISIN")
        
        # ISIN fehlt
        if not isin:
            skipped += 1
            error_details.append({
                "row_index": index,
                "error": "Spalte 'ISIN' nicht gefunden. Prüfe Header und Trennzeichen (;)."
            })
            continue

        # 1. Aktie finden oder anlegen
        aktie = Aktie.query.filter_by(isin=isin).first()
        if not aktie:
            try:
                info = get_company_info_cached(isin)
                aktie = Aktie(
                    isin=isin,
                    name=info.get("shortName") or row.get("Aktienname") or isin,
                    firma=info.get("longName") or row.get("Firma") or "Unbekannte Firma",
                    land=info.get("country"),
                    currency=info.get("currency")
                )
                db.session.add(aktie)
                db.session.flush()
            except Exception as e:
                skipped += 1
                error_details.append({"isin": isin, "error": f"Aktie konnte nicht angelegt werden: {str(e)}"})
                continue

        # 2. Transaktion anlegen
        try:
            raw_menge = str(row.get("Menge", "0")).replace(",", ".")
            raw_preis = str(row.get("Kaufpreis", "0")).replace(",", ".")
            raw_datum = row.get("Kaufdatum")

            if not raw_datum:
                raise ValueError("Spalte 'Kaufdatum' fehlt oder ist leer.")

            new_tx = Transaktion(
                portfolio_id=portfolio.id,
                aktie_id=aktie.id,
                menge=float(raw_menge),
                kaufpreis=float(raw_preis),
                kaufdatum=datetime.fromisoformat(raw_datum).date()
            )
            db.session.add(new_tx)
            created += 1
        except Exception as e:
            skipped += 1
            error_details.append({"isin": isin, "error": str(e)})

    db.session.commit()

    return jsonify({
        "message": "Import abgeschlossen",
        "portfolio_id": portfolio.id,
        "portfolio_name": portfolio.name,
        "created_transactions": created,
        "skipped_transactions": skipped,
        "errors": error_details
    }), 200

# Endpunkt für Pie-Chart-Daten bzgl. der Kategorie
@api_bp.route("/portfolios/<int:portfolio_id>/sector", methods=["GET"])
@jwt_required()
def get_portfolio_allocation(portfolio_id):
    # 1. Portfolio laden
    portfolio = Portfolio.query.get_or_404(portfolio_id)
    current_user_id = int(get_jwt_identity())
    
    if portfolio.user_id != current_user_id:
        return jsonify({"error": "Nicht autorisiert - Dieses Portfolio gehört dir nicht."}), 403

    # 2. Aggregierte Bestände (Mengen) pro Aktie holen
    rows = (
        db.session.query(
            Aktie.isin,
            Aktie.kategorie,
            func.coalesce(func.sum(Transaktion.menge), 0).label("menge_summe")
        )
        .join(Transaktion, Transaktion.aktie_id == Aktie.id)
        .filter(Transaktion.portfolio_id == portfolio_id)
        .group_by(Aktie.isin, Aktie.kategorie)
        .all()
    )

    if not rows:
        return jsonify({
            "portfolio_id": portfolio_id,
            "allocation": [],
            "total_value": 0,
            "message": "Keine Bestände in diesem Portfolio."
        }), 200

    # 3. Aktuelle Marktwerte berechnen und nach Kategorie gruppieren
    category_values = {}
    total_portfolio_value = 0.0

    for r in rows:
        qty = float(r.menge_summe)
        if qty <= 0:
            continue

        # Kategorie bestimmen
        cat_name = r.kategorie if r.kategorie else "Sonstige"
        
        # Aktuellen Kurs holen
        symbol = resolve_symbol_from_isin(r.isin)
        price = None
        if symbol:
            price, _ = get_price_and_prevclose(symbol)
        
        # Falls kein Kurs gefunden wurde, nehmen wir 0
        current_price = price if price is not None else 0.0
        market_value = qty * current_price

        # Werte summieren
        category_values[cat_name] = category_values.get(cat_name, 0.0) + market_value
        total_portfolio_value += market_value

    # 4. Prozentrechnung und Formatierung für das Piechart
    allocation_list = []
    
    if total_portfolio_value > 0:
        for cat, val in category_values.items():
            percentage = (val / total_portfolio_value) * 100
            allocation_list.append({
                "label": cat,
                "value": round(val, 2),
                "percentage": round(percentage, 2)
            })
    
    # Sortierung nach Größe
    allocation_list.sort(key=lambda x: x["percentage"], reverse=True)

    return jsonify({
        "portfolio_id": portfolio_id,
        "portfolio_name": portfolio.name,
        "total_value": round(total_portfolio_value, 2),
        "allocation": allocation_list
    }), 200

# Endpunkt für Pie-Chart-Daten bzgl. der Aktienallokation innerhalb eines Portfolios
@api_bp.route("/portfolios/<int:portfolio_id>/stock-allocation", methods=["GET"])
@jwt_required()
def get_portfolio_stock_allocation(portfolio_id):
    current_user_id = int(get_jwt_identity())
    
    # 1. Portfolio laden und Zugriff prüfen
    portfolio = Portfolio.query.get_or_404(portfolio_id)
    if portfolio.user_id != current_user_id:
        return jsonify({"error": "Nicht autorisiert."}), 403

    # 2. Bestände aggregieren (Menge pro Aktie)
    rows = (
        db.session.query(
            Aktie.name.label("name"),
            Aktie.isin.label("isin"),
            func.coalesce(func.sum(Transaktion.menge), 0).label("menge_summe")
        )
        .join(Transaktion, Transaktion.aktie_id == Aktie.id)
        .filter(Transaktion.portfolio_id == portfolio_id)
        .group_by(Aktie.name, Aktie.isin)
        .all()
    )

    if not rows:
        return jsonify({"portfolio_id": portfolio_id, "allocation": [], "total_value": 0}), 200

    # 3. Marktwerte berechnen
    stock_details = []
    total_portfolio_value = 0.0

    for r in rows:
        qty = float(r.menge_summe)
        if qty <= 0:
            continue
        
        # Aktuellen Kurs holen
        symbol = resolve_symbol_from_isin(r.isin)
        price, _ = get_price_and_prevclose(symbol) if symbol else (None, None)
        
        current_price = float(price) if price is not None else 0.0
        position_value = qty * current_price
        
        total_portfolio_value += position_value
        
        stock_details.append({
            "label": r.name, # Der Name der Aktie für das Chart-Label
            "isin": r.isin,
            "value": position_value
        })

    # 4. Prozentsätze berechnen und finalisieren
    allocation = []
    for stock in stock_details:
        percentage = (stock["value"] / total_portfolio_value * 100) if total_portfolio_value > 0 else 0
        allocation.append({
            "label": stock["label"],
            "isin": stock["isin"],
            "value": round(stock["value"], 2),
            "percentage": round(percentage, 2)
        })

    # Sortierung: Größte Positionen zuerst
    allocation.sort(key=lambda x: x["value"], reverse=True)

    return jsonify({
        "portfolio_id": portfolio_id,
        "portfolio_name": portfolio.name,
        "total_value": round(total_portfolio_value, 2),
        "allocation": allocation
    }), 200

# Endpunkt für Balkendiagramm bzgl. der allokation nach Ländern
@api_bp.route("/portfolios/<int:portfolio_id>/country-allocation", methods=["GET"])
@jwt_required()
def get_portfolio_country_allocation(portfolio_id):
    current_user_id = int(get_jwt_identity())
    
    # 1. Portfolio laden & Zugriff prüfen
    portfolio = Portfolio.query.get_or_404(portfolio_id)
    if portfolio.user_id != current_user_id:
        return jsonify({"error": "Nicht autorisiert."}), 403

    # 2. Bestände mit Länder-Info aggregieren
    rows = (
        db.session.query(
            Aktie.land.label("land"),
            Aktie.isin.label("isin"),
            func.coalesce(func.sum(Transaktion.menge), 0).label("menge_summe")
        )
        .join(Transaktion, Transaktion.aktie_id == Aktie.id)
        .filter(Transaktion.portfolio_id == portfolio_id)
        .group_by(Aktie.land, Aktie.isin)
        .all()
    )

    if not rows:
        return jsonify({"portfolio_id": portfolio_id, "results": []}), 200

    # 3. Marktwerte pro Land berechnen
    country_map = {}
    total_value = 0.0

    for r in rows:
        qty = float(r.menge_summe)
        if qty <= 0:
            continue
        
        # Aktuellen Kurs via yfinance Logik holen
        symbol = resolve_symbol_from_isin(r.isin)
        price, _ = get_price_and_prevclose(symbol) if symbol else (None, None)
        
        current_price = float(price) if price is not None else 0.0
        pos_value = qty * current_price
        
        # Land bestimmen (Fallback "Unbekannt")
        country = r.land if r.land else "Unbekannt"
        
        country_map[country] = country_map.get(country, 0.0) + pos_value
        total_value += pos_value

    # 4. Daten für das Balkendiagramm aufbereiten
    results = []
    for country, val in country_map.items():
        results.append({
            "country": country,
            "value": round(val, 2),
            "percentage": round((val / total_value * 100), 2) if total_value > 0 else 0
        })

    # Sortierung nach Wert absteigend
    results.sort(key=lambda x: x["value"], reverse=True)

    return jsonify({
        "portfolio_id": portfolio_id,
        "portfolio_name": portfolio.name,
        "total_value": round(total_value, 2),
        "results": results
    }), 200

#Portfolio Performance Linechart Endpoint

def _parse_bool(v: str) -> bool:
    return str(v or "").lower() in ("1", "true", "yes", "y", "on")

def _parse_range_to_dates(range_str: str):
    """
    Unterstützte ranges für die Linechart : - Default Range = 1 mo
    1mo, 3mo, 6mo, 1y, max
    """
    today = date.today()
    r = (range_str or "1mo").lower().strip()

    if r == "1mo":
        return today - timedelta(days=31), today
    if r == "3mo":
        return today - timedelta(days=93), today
    if r == "6mo":
        return today - timedelta(days=186), today
    if r == "1y":
        return today - timedelta(days=366), today
    if r == "ytd":
        return date(today.year, 1, 1), today
    if r == "max":
        # Fallback: wenn max gewünscht, aber wir keine Portfolio-Startdaten kennen - default = 5y
        return today - timedelta(days=365 * 5 + 2), today

    # Unbekannt -> default 1mo
    return today - timedelta(days=31), today

def _safe_float(x, default=None):
    try:
        if x is None:
            return default
        # NaN check: x != x
        if isinstance(x, float) and x != x:
            return default
        return float(x)
    except Exception:
        return default

@api_bp.route("/portfolios/<int:portfolio_id>/performance-linechart", methods=["GET"])
@jwt_required()
def portfolio_performance_linechart(portfolio_id: int):
    """
    Ticket #45: Performance-Linechart
    - Portfolio Wert je Tag (über Zeitraum)
    - Wert je Aktie je Tag (inkl. Stückzahl je Tag)
    - Wert je Transaktion je Tag

    Parameter:
    - range=1mo|3mo|6mo|1y|ytd|max   (default = 1 Monat!!)
    - start=YYYY-MM-DD              (optional, überschreibt range)
    - end=YYYY-MM-DD                (optional, default heute)
    - include_stocks=1              (default 1) -> per-Stock Serien
    - include_transactions=1        (default 1) -> per-TX Serien
    - include_positions_snapshot=0  (default 0) -> Positionen je Tag (kann zu groß werden)
    """

    portfolio = Portfolio.query.get_or_404(portfolio_id)

    # User berechetigt für Portfolio?
    current_user_id = int(get_jwt_identity())
    if portfolio.user_id != current_user_id:
        return jsonify({"error": "Not authorized - Not your portfolio."}), 403

    # Zeitraum
    range_str = request.args.get("range", "1mo")
    start_q = request.args.get("start")
    end_q = request.args.get("end")

    if start_q:
        try:
            start_date = date.fromisoformat(start_q)
        except Exception:
            return jsonify({"error": "Invalid 'start' date format. Use YYYY-MM-DD."}), 400
    else:
        start_date, _ = _parse_range_to_dates(range_str)

    if end_q:
        try:
            end_date = date.fromisoformat(end_q)
        except Exception:
            return jsonify({"error": "Invalid 'end' date format. Use YYYY-MM-DD."}), 400
    else:
        end_date = date.today()

    if start_date > end_date:
        return jsonify({"error": "'start' must be <= 'end'."}), 400

    include_stocks = _parse_bool(request.args.get("include_stocks", "1"))
    include_transactions = _parse_bool(request.args.get("include_transactions", "1"))
    include_positions_snapshot = _parse_bool(request.args.get("include_positions_snapshot", "0"))

    # Transaktionen laden!
    txs = (
        db.session.query(Transaktion)
        .options(joinedload(Transaktion.aktie))
        .filter(
            Transaktion.portfolio_id == portfolio_id,
            Transaktion.kaufdatum <= end_date
        )
        .order_by(Transaktion.kaufdatum.asc(), Transaktion.id.asc())
        .all()
    )

    if not txs:
        return jsonify({
            "portfolio_id": portfolio_id,
            "portfolio_name": portfolio.name,
            "range": range_str,
            "start": start_date.isoformat(),
            "end": end_date.isoformat(),
            "currency": "USD",
            "series": [],
            "per_stock": {} if include_stocks else None,
            "per_transaction": [] if include_transactions else None,
            "meta": {
                "note": "Keine Transaktionen im Zeitraum.",
                "mapped_symbols": 0,
            }
        }), 200

    stock_bucket = {}
    unresolved = []

    for tx in txs:
        isin = (tx.aktie.isin or "").strip()
        if not isin:
            unresolved.append({"tx_id": int(tx.id), "reason": "missing_isin"})
            continue

        symbol = resolve_symbol_from_isin(isin)
        if not symbol:
            unresolved.append({"tx_id": int(tx.id), "isin": isin, "reason": "no_symbol_for_isin"})
            continue

        b = stock_bucket.get(isin)
        if not b:
            stock_bucket[isin] = {
                "isin": isin,
                "symbol": symbol,
                "aktie_id": int(tx.aktie_id),
                "name": tx.aktie.name,
                "txs": []
            }
            b = stock_bucket[isin]

        b["txs"].append({
            "tx_id": int(tx.id),
            "kaufdatum": tx.kaufdatum,
            "menge": float(tx.menge),
            "kaufpreis": float(tx.kaufpreis),
        })

    if not stock_bucket:
        return jsonify({
            "portfolio_id": portfolio_id,
            "portfolio_name": portfolio.name,
            "range": range_str,
            "start": start_date.isoformat(),
            "end": end_date.isoformat(),
            "currency": "USD",
            "series": [],
            "per_stock": {} if include_stocks else None,
            "per_transaction": [] if include_transactions else None,
            "meta": {
                "note": "Keine Aktien konnten via ISIN->Symbol gemappt werden.",
                "unresolved": unresolved[:200],
            }
        }), 200

    yf_start = start_date.isoformat()
    yf_end = (end_date + timedelta(days=1)).isoformat()

    isin_to_symbol = {isin: b["symbol"] for isin, b in stock_bucket.items()}
    symbols = sorted(list(set(isin_to_symbol.values())))

    try:
        df = yf.download(
            tickers=symbols,
            start=yf_start,
            end=yf_end,
            interval="1d",
            auto_adjust=False,
            progress=False,
            threads=True,
            group_by="ticker"
        )
    except Exception as e:
        return jsonify({"error": "Error fetching historical prices", "detail": str(e)}), 500

    if df is None or getattr(df, "empty", True):
        return jsonify({"error": "No historical prices returned from Yahoo"}), 502

    try:
        trading_days = [ts.date() for ts in df.index]
    except Exception:
        trading_days = []

    if not trading_days:
        return jsonify({"error": "No trading days in price data"}), 502

    # Hilfsfunktion: close-price je symbol und day holen
    def _get_close(symbol: str, day: date):
        try:
            if len(symbols) == 1 and "Close" in df.columns:
                # Single ticker -> normale Spalten
                row = df.loc[str(day)]
                return _safe_float(row.get("Close"))
            else:
                # Multi ticker -> columns MultiIndex: (symbol, field)
                row = df.loc[str(day)]
                val = row.get((symbol, "Close"))
                return _safe_float(val)
        except Exception:
            return None

    # --- pro Stock: qty curve bauen (cumulative) ---
    per_stock_series = {}  # isin -> {"isin","symbol","name","series":[{date,qty,value}]}
    total_series = []      # [{date, total_value}]
    per_day_positions = [] # optional: [{date, positions:[...]}]

    delta_by_isin = {}
    for isin, b in stock_bucket.items():
        d = {}
        for tx in b["txs"]:
            dday = tx["kaufdatum"]
            if dday < start_date:
                continue
            d[dday] = d.get(dday, 0.0) + float(tx["menge"])
        delta_by_isin[isin] = d

    initial_qty_by_isin = {}
    for isin, b in stock_bucket.items():
        init_qty = 0.0
        for tx in b["txs"]:
            if tx["kaufdatum"] < start_date:
                init_qty += float(tx["menge"])
        initial_qty_by_isin[isin] = init_qty

    # Berechnen alles in einer Schleife pro day, dann pro stock
    running_qty = {isin: float(initial_qty_by_isin.get(isin) or 0.0) for isin in stock_bucket.keys()}

    per_transaction = []
    tx_lookup = []
    if include_transactions:
        for isin, b in stock_bucket.items():
            sym = b["symbol"]
            for tx in b["txs"]:
                tx_lookup.append({
                    "tx_id": int(tx["tx_id"]),
                    "isin": isin,
                    "symbol": sym,
                    "aktie_id": int(b["aktie_id"]),
                    "name": b.get("name"),
                    "kaufdatum": tx["kaufdatum"],
                    "menge": float(tx["menge"]),
                    "kaufpreis": float(tx["kaufpreis"]),
                })

    if include_stocks:
        for isin, b in stock_bucket.items():
            per_stock_series[isin] = {
                "isin": isin,
                "symbol": b["symbol"],
                "aktie_id": int(b["aktie_id"]),
                "name": b.get("name"),
                "series": []
            }

    for day in trading_days:
        day_total = 0.0
        day_positions = []

        for isin, b in stock_bucket.items():
            delta_map = delta_by_isin.get(isin) or {}
            if day in delta_map:
                running_qty[isin] += float(delta_map[day] or 0.0)

            qty = float(running_qty.get(isin) or 0.0)

            sym = b["symbol"]
            close = _get_close(sym, day)

            # Wenn close fehlt -> Wert 0 (oder skip)
            if close is None:
                value = 0.0
            else:
                value = qty * float(close)

            day_total += value

            if include_stocks:
                per_stock_series[isin]["series"].append({
                    "date": day.isoformat(),
                    "qty": round(qty, 6),
                    "value": round(value, 2),
                    "close": round(close, 6) if close is not None else None
                })

            if include_positions_snapshot:
                day_positions.append({
                    "isin": isin,
                    "symbol": sym,
                    "qty": round(qty, 6),
                    "value": round(value, 2),
                })

        total_series.append({
            "date": day.isoformat(),
            "total_value": round(day_total, 2)
        })

        if include_positions_snapshot:
            # große Payload -> nur wenn explizit gewünscht
            per_day_positions.append({
                "date": day.isoformat(),
                "positions": day_positions
            })

    if include_transactions and tx_lookup:
        for tx in tx_lookup:
            sym = tx["symbol"]
            start_tx_day = tx["kaufdatum"]
            menge = float(tx["menge"])

            series = []
            for day in trading_days:
                if day < start_tx_day:
                    continue
                close = _get_close(sym, day)
                val = (menge * float(close)) if close is not None else 0.0
                series.append({
                    "date": day.isoformat(),
                    "value": round(val, 2),
                    "close": round(close, 6) if close is not None else None
                })

            per_transaction.append({
                "tx_id": int(tx["tx_id"]),
                "aktie_id": int(tx["aktie_id"]),
                "isin": tx["isin"],
                "symbol": tx["symbol"],
                "name": tx.get("name"),
                "kaufdatum": start_tx_day.isoformat(),
                "menge": round(menge, 6),
                "kaufpreis": round(float(tx["kaufpreis"]), 6),
                "series": series
            })

    return jsonify({
        "portfolio_id": portfolio_id,
        "portfolio_name": portfolio.name,
        "range": range_str,
        "start": start_date.isoformat(),
        "end": end_date.isoformat(),
        "interval": "1d",
        "currency": "USD",

        # Linechart (Gesamt)
        "series": total_series,

        # Optional: je Aktie
        "per_stock": per_stock_series if include_stocks else None,

        # Optional: je Transaktion
        "per_transaction": per_transaction if include_transactions else None,

        # Optional: Positionen pro Tag (sehr groß)
        "positions_by_day": per_day_positions if include_positions_snapshot else None,

        "meta": {
            "mapped_symbols": len(symbols),
            "isins": len(stock_bucket),
            "transactions_used": len(txs),
            "unresolved_count": len(unresolved),
            "unresolved": unresolved[:200],
            "note": "Kurse: Yahoo (yfinance) Close 1d; Bestände: kumuliert aus Transaktionen bis zum jeweiligen Tag."
        }
    }), 200