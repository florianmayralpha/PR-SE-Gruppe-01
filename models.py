from datetime import datetime
import enum

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import Enum as SAEnum, Numeric
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()


# ----- Enums -----

class SenderEnum(enum.Enum):
    USER = "User"
    AI = "AI"


class ChatTypeEnum(enum.Enum):
    PORTFOLIO = "Portfolio"
    AKTIE = "Aktie"


# ----- User -----

class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    firstname = db.Column(db.String(120), nullable=False)
    lastname = db.Column(db.String(120), nullable=False)
    # In echt würdest du hier ein Hash speichern
    password = db.Column(db.String(255), nullable=False)

    portfolios = db.relationship(
        "Portfolio",
        back_populates="user",
        cascade="all, delete-orphan",
    )
    watchlist_entries = db.relationship(
        "Watchlist",
        back_populates="user",
        cascade="all, delete-orphan",
    )
    chats = db.relationship(
        "Chatverlauf",
        back_populates="user",
        cascade="all, delete-orphan",
    )

    def set_password(self, raw_password: str):
        self.password = generate_password_hash(raw_password)

    def check_password(self, raw_password: str) -> bool:
        return check_password_hash(self.password, raw_password)

    def to_dict(self):
        return {
            "id": self.id,
            "username": self.username,
            "firstname": self.firstname,
            "lastname": self.lastname,
        }


# ----- Portfolio -----

class Portfolio(db.Model):
    __tablename__ = "portfolios"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)

    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    user = db.relationship("User", back_populates="portfolios")

    transactions = db.relationship(
        "Transaktion",
        back_populates="portfolio",
        cascade="all, delete-orphan",
    )

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "user_id": self.user_id,
        }


# ----- Aktie (Stock) -----

class Aktie(db.Model):
    __tablename__ = "aktien"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    isin = db.Column(db.String(50), unique=True, nullable=False)
    firma = db.Column(db.String(255), nullable=False)
    ausschüttungsart = db.Column(db.String(100))
    kategorie = db.Column(db.String(100))
    land = db.Column(db.String(100))
    beschreibung = db.Column(db.Text)

    ebitda = db.Column(Numeric(18, 2))
    nettogewinn = db.Column(Numeric(18, 2))
    umsatz = db.Column(Numeric(18, 2))
    currency = db.Column(db.String(10))
    unternehmenswert = db.Column(Numeric(18, 2))

    transactions = db.relationship(
        "Transaktion",
        back_populates="aktie",
        cascade="all, delete-orphan",
    )
    watchlist_entries = db.relationship(
        "Watchlist",
        back_populates="aktie",
        cascade="all, delete-orphan",
    )

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "isin": self.isin,
            "firma": self.firma,
            "ausschüttungsart": self.ausschüttungsart,
            "kategorie": self.kategorie,
            "land": self.land,
            "beschreibung": self.beschreibung,
            "ebitda": float(self.ebitda) if self.ebitda is not None else None,
            "nettogewinn": float(self.nettogewinn) if self.nettogewinn is not None else None,
            "umsatz": float(self.umsatz) if self.umsatz is not None else None,
            "currency": self.currency,
            "unternehmenswert": float(self.unternehmenswert)
            if self.unternehmenswert is not None
            else None,
        }


# ----- Transaktionen -----

class Transaktion(db.Model):
    __tablename__ = "transaktionen"

    id = db.Column(db.Integer, primary_key=True)
    menge = db.Column(Numeric(18, 4), nullable=False)
    kaufpreis = db.Column(Numeric(18, 4), nullable=False)
    kaufdatum = db.Column(db.Date, nullable=False)

    aktie_id = db.Column(db.Integer, db.ForeignKey("aktien.id"), nullable=False)
    portfolio_id = db.Column(
        db.Integer, db.ForeignKey("portfolios.id"), nullable=False
    )

    aktie = db.relationship("Aktie", back_populates="transactions")
    portfolio = db.relationship("Portfolio", back_populates="transactions")

    def to_dict(self):
        return {
            "id": self.id,
            "menge": float(self.menge),
            "kaufpreis": float(self.kaufpreis),
            "kaufdatum": self.kaufdatum.isoformat(),
            "aktie_id": self.aktie_id,
            "portfolio_id": self.portfolio_id,
        }


# ----- Watchlist (User <-> Aktie) -----

class Watchlist(db.Model):
    """
    Jeder Datensatz ist ein Eintrag:
    User beobachtet eine bestimmte Aktie.
    """

    __tablename__ = "watchlist"

    id = db.Column(db.Integer, primary_key=True)

    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    aktie_id = db.Column(db.Integer, db.ForeignKey("aktien.id"), nullable=False)

    user = db.relationship("User", back_populates="watchlist_entries")
    aktie = db.relationship("Aktie", back_populates="watchlist_entries")

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "aktie_id": self.aktie_id,
        }


# ----- Chatverlauf -----

class Chatverlauf(db.Model):
    __tablename__ = "chatverlauf"

    id = db.Column(db.Integer, primary_key=True)

    # Type = Portfolio | Aktie
    type = db.Column(SAEnum(ChatTypeEnum, name="chat_type_enum"), nullable=False)
    # foreignId = id von Portfolio oder Aktie (abh. von type)
    foreign_id = db.Column(db.Integer, nullable=False)

    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    user = db.relationship("User", back_populates="chats")

    entries = db.relationship(
        "ChatEntry",
        back_populates="chat",
        cascade="all, delete-orphan",
    )

    def to_dict(self):
        return {
            "id": self.id,
            "type": self.type.value,
            "foreign_id": self.foreign_id,
            "user_id": self.user_id,
        }


# ----- ChatEntry -----

class ChatEntry(db.Model):
    __tablename__ = "chat_entries"

    id = db.Column(db.Integer, primary_key=True)

    sender = db.Column(SAEnum(SenderEnum, name="sender_enum"), nullable=False)
    text = db.Column(db.Text, nullable=False)
    datetime = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    chat_id = db.Column(db.Integer, db.ForeignKey("chatverlauf.id"), nullable=False)
    chat = db.relationship("Chatverlauf", back_populates="entries")

    def to_dict(self):
        return {
            "id": self.id,
            "sender": self.sender.value,
            "text": self.text,
            "datetime": self.datetime.isoformat(),
            "chat_id": self.chat_id,
        }

#   DTOs (Aggregierte Portfolio-Kennzahlen)

from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any

@dataclass
class PortfolioPerformanceDTO:
    portfolio_id: int
    portfolio_name: str
    total_value: float
    absolute_return_eur: float
    absolute_return_pct: Optional[float]
    daily_change_pct: Optional[float]
    currency: str = "EUR"
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "portfolio_id": self.portfolio_id,
            "portfolio_name": self.portfolio_name,
            "total_value": self.total_value,
            "absolute_return_eur": self.absolute_return_eur,
            "absolute_return_pct": self.absolute_return_pct,
            "daily_change_pct": self.daily_change_pct,
            "currency": self.currency,
            "meta": self.meta,
        }


@dataclass
class AggregatedPortfolioPerformanceDTO:
    user_id: int
    portfolios_count: int
    total_value: float
    absolute_return_eur: float
    absolute_return_pct: Optional[float]
    daily_change_pct: Optional[float]
    currency: str = "EUR"
    portfolios: List[Dict[str, Any]] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "user_id": self.user_id,
            "portfolios_count": self.portfolios_count,
            "total_value": self.total_value,
            "absolute_return_eur": self.absolute_return_eur,
            "absolute_return_pct": self.absolute_return_pct,
            "daily_change_pct": self.daily_change_pct,
            "currency": self.currency,
            "portfolios": self.portfolios,
            "meta": self.meta,
        }
