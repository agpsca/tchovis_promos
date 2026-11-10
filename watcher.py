#!/usr/bin/env python3
"""Monitora preços em lojas e envia alertas pelo Telegram."""

from __future__ import annotations

import argparse
import asyncio
import gzip
import html
import http.cookiejar
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from playwright.async_api import BrowserContext, Page, async_playwright

ROOT = Path(__file__).resolve().parent
STATE_DIR = Path(os.environ.get("TCHOVIS_STATE_DIR", str(ROOT / "state")))
DB_PATH = STATE_DIR / "prices.sqlite3"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)

# Transporte padrao por loja. A KaBuM entrega o catalogo em __NEXT_DATA__ e
# dispensa navegador. A Amazon e a Magalu bloqueiam HTTP puro quando a
# origem e IP de datacenter, entao exigem navegador.
DEFAULT_TRANSPORT = {
    "kabum": "http",
    "amazon": "browser",
    "magalu": "browser",
}

HTTP_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "pt-BR,pt;q=0.9",
    "Accept-Encoding": "gzip",
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
}


def http_open(
    url: str,
    opener: urllib.request.OpenerDirector | None = None,
    payload: Any = None,
    timeout: int = 30,
) -> str:
    """GET ou POST simples com gzip, cabecalhos de navegador e timeout."""
    data = None
    headers = dict(HTTP_HEADERS)
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers)
    runner = opener.open if opener else urllib.request.urlopen
    with runner(request, timeout=timeout) as response:
        raw = response.read()
        if response.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    return raw.decode("utf-8", errors="replace")


def http_json(url: str, **kwargs: Any) -> Any:
    return json.loads(http_open(url, **kwargs))


def next_data(markup: str) -> dict[str, Any]:
    match = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', markup, re.S
    )
    if not match:
        raise RuntimeError("__NEXT_DATA__ ausente na resposta")
    return json.loads(match.group(1))


def as_mapping(value: Any) -> dict[str, Any]:
    """Aceita objeto ou string JSON.

    A KaBuM usa as duas formas no mesmo campo: uma busca comum entrega
    pageProps.data como objeto, e um termo que cai numa categoria entrega
    a mesma estrutura serializada como texto.
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    return value if isinstance(value, dict) else {}


def kabum_catalog_server(payload: dict[str, Any]) -> dict[str, Any]:
    page_props = as_mapping(as_mapping(payload.get("props")).get("pageProps"))
    return as_mapping(as_mapping(page_props.get("data")).get("catalogServer"))


def kabum_catalog(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Itens da busca da KaBuM a partir do __NEXT_DATA__ ja decodificado."""
    items = kabum_catalog_server(payload).get("data") or []
    return [item for item in items if isinstance(item, dict)]


def kabum_redirect(payload: dict[str, Any]) -> str | None:
    """Categoria para onde o termo aponta, quando a busca vem vazia.

    Termos como "air fryer" casam com uma categoria: a loja devolve lista
    vazia e um redirect, esperando que o cliente siga para a categoria.
    """
    target = kabum_catalog_server(payload).get("redirect")
    return target if isinstance(target, str) and target.startswith("/") else None


def kabum_url(path: str) -> str:
    return f"https://www.kabum.com.br{path}?page_size=100"


def new_opener() -> urllib.request.OpenerDirector:
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def kabum_offer_from_item(
    item: dict[str, Any], filters: dict[str, list[str]] | None = None
) -> Offer | None:
    title = item.get("name") or ""
    if ignored_title(title, filters) or not item.get("available"):
        return None
    offer = item.get("offer") or {}
    price = offer.get("priceWithDiscount") or item.get("priceWithDiscount")
    if not price:
        return None
    code = item.get("code")
    return Offer(
        key=f"kabum-{code}", store="KaBuM", title=title,
        url=f"https://www.kabum.com.br/produto/{code}", price=float(price),
        seller=item.get("sellerName"), source="search",
        note="Preco no Pix; frete nao calculado",
    )


def kabum_search_http(
    query: str, filters: dict[str, list[str]] | None = None
) -> list[Offer]:
    slug = query.replace(" ", "-")
    payload = next_data(http_open(kabum_url(f"/busca/{slug}")))
    items = kabum_catalog(payload)
    if not items:
        target = kabum_redirect(payload)
        if target:
            items = kabum_catalog(next_data(http_open(kabum_url(target))))
    offers = []
    for item in items:
        offer = kabum_offer_from_item(item, filters)
        if offer:
            offers.append(offer)
    return offers


def kabum_product_http(product: dict[str, Any]) -> Offer:
    page_props = as_mapping(
        as_mapping(next_data(http_open(product["url"])).get("props")).get("pageProps")
    )
    item = as_mapping(page_props.get("product"))
    prices = as_mapping(item.get("prices"))
    title = item.get("title") or product["name"]
    return Offer(
        key=product["id"], store="KaBuM", title=title, url=product["url"],
        price=float(prices.get("priceWithDiscount") or item.get("price") or 0),
        available=bool(item.get("available")), shipping=product.get("shipping_cost"),
        seller=item.get("sellerName"),
        note="Preco no Pix; frete nao calculado"
        if product.get("shipping_cost") is None else None,
    )


def transport_for(entry: dict[str, Any]) -> str:
    store = str(entry.get("store", "")).lower()
    return str(entry.get("transport") or DEFAULT_TRANSPORT.get(store, "browser"))


def collect_http(
    products: list[dict[str, Any]],
    searches: list[dict[str, Any]],
    config: dict[str, Any],
) -> tuple[dict[str, Offer], list[str]]:
    """Coleta sem navegador. Hoje so a KaBuM permite."""
    offers: dict[str, Offer] = {}
    errors: list[str] = []
    for product in products:
        store = str(product["store"]).lower()
        for attempt in range(2):
            try:
                if store == "kabum":
                    offer = kabum_product_http(product)
                else:
                    raise ValueError(f"sem transporte HTTP para {store}")
                offers[offer_slot(offer)] = offer
                break
            except Exception as error:  # noqa: BLE001
                if attempt:
                    errors.append(f"{product.get('id')} (http): {error}")
    for search in searches:
        store = str(search["store"]).lower()
        for attempt in range(2):
            try:
                if store == "kabum":
                    found = kabum_search_http(search["query"], title_filters(search, config))
                else:
                    raise ValueError(f"sem transporte HTTP para {store}")
                for offer in found:
                    offer.watch_id = search.get("watch_id")
                    offers.setdefault(offer_slot(offer), offer)
                break
            except Exception as error:  # noqa: BLE001
                if attempt:
                    errors.append(f"busca {search.get('query')} em {store} (http): {error}")
    return offers, errors


@dataclass
class Offer:
    key: str
    store: str
    title: str
    url: str
    price: float
    available: bool = True
    shipping: float | None = None
    seller: str | None = None
    coupon: str | None = None
    source: str = "product"
    note: str | None = None
    watch_id: int | None = None

    @property
    def final_price(self) -> float:
        return self.price + (self.shipping or 0)


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def money(value: str | int | float | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = re.sub(r"[^\d,]", "", value)
    if not cleaned:
        return None
    try:
        return float(cleaned.replace(",", "."))
    except ValueError:
        return None


def normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:180]


DEFAULT_EXCLUDE_TERMS = (
    "recondicionado", "usado", "renewed", "renovado", "open box", "openbox",
)


def title_filters(entry: dict[str, Any], config: dict[str, Any]) -> dict[str, list[str]]:
    """Termos de titulo de uma busca.

    A propria entrada manda quando traz algum dos campos; caso contrario
    vale o padrao em search_filters do config.json. E assim que uma busca
    fixa pode exigir um termo no titulo sem que as consultas do bot, de
    qualquer categoria, herdem essa exigencia.
    """
    keys = ("require_any", "require_all", "exclude")
    source = entry if any(key in entry for key in keys) else config.get("search_filters", {})
    return {
        "require_any": [str(term).lower() for term in source.get("require_any", [])],
        "require_all": [str(term).lower() for term in source.get("require_all", [])],
        "exclude": [str(term).lower() for term in source.get("exclude", DEFAULT_EXCLUDE_TERMS)],
    }


def ignored_title(title: str, filters: dict[str, list[str]] | None = None) -> bool:
    """Descarta um titulo pelos termos exigidos e proibidos.

    Sem filtros, barra apenas usados e recondicionados.
    """
    lowered = title.lower()
    active = filters or {}
    if any(term in lowered for term in active.get("exclude", DEFAULT_EXCLUDE_TERMS)):
        return True
    require_any = active.get("require_any") or []
    if require_any and not any(term in lowered for term in require_any):
        return True
    return any(term not in lowered for term in active.get("require_all", []))


def offer_slot(offer: Offer) -> str:
    """Chave de deduplicacao. A mesma oferta pode servir a duas consultas."""
    return f"{offer.watch_id or 0}:{offer.key}"


@dataclass
class Watch:
    """Consulta monitorada, cadastrada pelo Telegram."""

    id: int
    chat_id: str
    query: str
    stores: list[str]
    maximum_price: float | None = None
    require_terms: list[str] = None  # type: ignore[assignment]
    exclude_terms: list[str] = None  # type: ignore[assignment]
    active: bool = True

    def __post_init__(self) -> None:
        self.require_terms = self.require_terms or []
        self.exclude_terms = self.exclude_terms or []

    def as_searches(self) -> list[dict[str, Any]]:
        """Entradas de busca equivalentes, uma por loja."""
        return [
            {
                "store": store,
                "query": self.query,
                "watch_id": self.id,
                "require_all": self.require_terms,
                "exclude": self.exclude_terms or list(DEFAULT_EXCLUDE_TERMS),
            }
            for store in self.stores
        ]

    def describe(self) -> str:
        limit = format_brl(self.maximum_price) if self.maximum_price else "qualquer queda"
        parts = [f"<b>#{self.id}</b> {html.escape(self.query)}", f"limite: {limit}"]
        parts.append("lojas: " + ", ".join(self.stores))
        if self.require_terms:
            parts.append("exige: " + ", ".join(self.require_terms))
        if self.exclude_terms:
            parts.append("exclui: " + ", ".join(self.exclude_terms))
        if not self.active:
            parts.append("PAUSADA")
        return "\n   ".join(parts)


def split_terms(raw: str | None) -> list[str]:
    return [term.strip() for term in (raw or "").split(",") if term.strip()]


class History:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS observations (
                id INTEGER PRIMARY KEY,
                offer_key TEXT NOT NULL,
                checked_at TEXT NOT NULL,
                store TEXT NOT NULL,
                title TEXT NOT NULL,
                price REAL NOT NULL,
                shipping REAL,
                final_price REAL NOT NULL,
                available INTEGER NOT NULL,
                payload TEXT NOT NULL
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS alert_state (
                offer_key TEXT PRIMARY KEY,
                last_price REAL NOT NULL,
                last_available INTEGER NOT NULL,
                sent_at TEXT NOT NULL
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS watches (
                id INTEGER PRIMARY KEY,
                chat_id TEXT NOT NULL,
                query TEXT NOT NULL,
                stores TEXT NOT NULL,
                maximum_price REAL,
                require_terms TEXT,
                exclude_terms TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        self.connection.commit()

    # --- consultas monitoradas -----------------------------------------

    def add_watch(
        self,
        chat_id: str,
        query: str,
        stores: list[str],
        maximum_price: float | None = None,
        require_terms: list[str] | None = None,
        exclude_terms: list[str] | None = None,
    ) -> Watch:
        cursor = self.connection.execute(
            """INSERT INTO watches
               (chat_id, query, stores, maximum_price, require_terms, exclude_terms, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                str(chat_id),
                query,
                ",".join(stores),
                maximum_price,
                ",".join(require_terms or []),
                ",".join(exclude_terms or []),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self.connection.commit()
        return Watch(
            id=int(cursor.lastrowid or 0), chat_id=str(chat_id), query=query,
            stores=stores, maximum_price=maximum_price,
            require_terms=require_terms or [], exclude_terms=exclude_terms or [],
        )

    def watches(self, active_only: bool = True) -> list[Watch]:
        sql = (
            "SELECT id, chat_id, query, stores, maximum_price, require_terms, "
            "exclude_terms, active FROM watches"
        )
        if active_only:
            sql += " WHERE active = 1"
        return [
            Watch(
                id=row[0], chat_id=row[1], query=row[2], stores=split_terms(row[3]),
                maximum_price=row[4], require_terms=split_terms(row[5]),
                exclude_terms=split_terms(row[6]), active=bool(row[7]),
            )
            for row in self.connection.execute(sql + " ORDER BY id")
        ]

    def watch(self, watch_id: int) -> Watch | None:
        return next((item for item in self.watches(active_only=False) if item.id == watch_id), None)

    def set_watch_active(self, watch_id: int, active: bool) -> bool:
        cursor = self.connection.execute(
            "UPDATE watches SET active = ? WHERE id = ?", (int(active), watch_id)
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def remove_watch(self, watch_id: int) -> bool:
        cursor = self.connection.execute("DELETE FROM watches WHERE id = ?", (watch_id,))
        self.connection.commit()
        return cursor.rowcount > 0

    def state_get(self, key: str) -> str | None:
        row = self.connection.execute(
            "SELECT value FROM bot_state WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else None

    def state_set(self, key: str, value: str) -> None:
        self.connection.execute(
            """INSERT INTO bot_state (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (key, value),
        )
        self.connection.commit()

    # --- historico -----------------------------------------------------

    def record(self, offer: Offer) -> None:
        self.connection.execute(
            """INSERT INTO observations
               (offer_key, checked_at, store, title, price, shipping, final_price, available, payload)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                offer.key,
                datetime.now(timezone.utc).isoformat(),
                offer.store,
                offer.title,
                offer.price,
                offer.shipping,
                offer.final_price,
                int(offer.available),
                json.dumps(asdict(offer), ensure_ascii=False),
            ),
        )
        self.connection.commit()

    @staticmethod
    def alert_key(offer: Offer) -> str:
        """Chave do estado de alerta.

        Ofertas de uma consulta monitorada ganham um prefixo proprio, para
        que duas consultas nao silenciem uma a outra. Ofertas dos produtos
        fixos do config.json mantem a chave antiga, preservando o estado
        gravado antes das consultas existirem.
        """
        return f"w{offer.watch_id}:{offer.key}" if offer.watch_id else offer.key

    def should_alert(
        self, offer: Offer, minimum_drop: float, alert_on_first: bool = True
    ) -> bool:
        row = self.connection.execute(
            "SELECT last_price, last_available FROM alert_state WHERE offer_key = ?",
            (self.alert_key(offer),),
        ).fetchone()
        if row is None:
            return alert_on_first
        last_price, last_available = row
        return (not last_available and offer.available) or offer.final_price <= last_price - minimum_drop

    def mark_alerted(self, offer: Offer) -> None:
        self.connection.execute(
            """INSERT INTO alert_state (offer_key, last_price, last_available, sent_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(offer_key) DO UPDATE SET
                 last_price=excluded.last_price,
                 last_available=excluded.last_available,
                 sent_at=excluded.sent_at""",
            (
                self.alert_key(offer),
                offer.final_price,
                int(offer.available),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self.connection.commit()


async def open_page(context: BrowserContext, url: str) -> Page:
    last_error: Exception | None = None
    for _ in range(2):
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_timeout(2_500)
            return page
        except Exception as error:  # noqa: BLE001
            last_error = error
            await page.close()
    raise RuntimeError(f"Falha ao abrir {url}: {last_error}")


async def body_text(page: Page) -> str:
    return await page.locator("body").inner_text()


async def amazon_product(context: BrowserContext, product: dict[str, Any]) -> Offer:
    page = await open_page(context, product["url"])
    try:
        # A Amazon as vezes demora ou serve variacao de layout. Em vez de
        # derrubar a consulta, cai para o titulo da aba e segue.
        title = product.get("name", "")
        heading = page.locator("#productTitle")
        try:
            if await heading.count():
                title = (await heading.first.inner_text(timeout=15_000)).strip()
            else:
                raw = await page.title()
                cleaned = re.sub(r"\s*[:|]\s*Amazon\.com\.br.*$", "", raw).strip()
                title = cleaned or title
        except Exception:  # noqa: BLE001
            pass
        text = await body_text(page)
        selectors = (
            "#corePriceDisplay_desktop_feature_div .priceToPay .a-offscreen",
            "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
            "#corePrice_feature_div .a-offscreen",
        )
        price = None
        for selector in selectors:
            locator = page.locator(selector)
            if await locator.count():
                price = money(await locator.first.text_content())
                if price is not None:
                    break
        unavailable = "não disponível" in text.lower() or "indisponível" in text.lower()
        seller_match = re.search(r"Enviado\s*/\s*Vendido\s*\n([^\n]+)", text, re.I)
        coupon_area = ""
        for selector in ("#promoPriceBlockMessage_feature_div", "[id^='couponText']"):
            locator = page.locator(selector)
            if await locator.count():
                coupon_area += " " + await locator.first.inner_text()
        return Offer(
            key=product["id"], store="Amazon", title=title, url=product["url"],
            price=price or 0, available=bool(price) and not unavailable,
            shipping=0 if "ENTREGA GRÁTIS" in text.upper() else product.get("shipping_cost"),
            seller=seller_match.group(1).strip() if seller_match else None,
            coupon=coupon_area.strip() or None,
        )
    finally:
        await page.close()


async def magalu_product(context: BrowserContext, product: dict[str, Any]) -> Offer:
    page = await open_page(context, product["url"])
    try:
        text = await body_text(page)
        # O titulo vem do h1 da pagina. Se ele nao estiver lá, fica o nome
        # configurado na entrada.
        title = str(product.get("name") or "")
        try:
            heading = page.locator("h1")
            if await heading.count():
                title = (await heading.first.inner_text(timeout=15_000)).strip() or title
        except Exception:  # noqa: BLE001
            pass
        price_match = re.search(r"Preço\s+R\$\s*([\d.]+,\d{2})", text, re.I)
        seller_match = re.search(r"Vendido por ([^\n]+)", text, re.I)
        return Offer(
            key=product["id"], store="Magalu", title=title, url=product["url"],
            price=money(price_match.group(1)) if price_match else 0,
            available="adicionar à sacola" in text.lower() or "comprar agora" in text.lower(),
            shipping=product.get("shipping_cost"), seller=seller_match.group(1).strip() if seller_match else None,
        )
    finally:
        await page.close()


async def kabum_product(context: BrowserContext, product: dict[str, Any]) -> Offer:
    page = await open_page(context, product["url"])
    try:
        raw = await page.locator("script#__NEXT_DATA__").text_content()
        page_props = as_mapping(
            as_mapping(json.loads(raw or "{}").get("props")).get("pageProps")
        )
        item = as_mapping(page_props.get("product"))
        prices = as_mapping(item.get("prices"))
        price = prices.get("priceWithDiscount") or item.get("price") or 0
        title = item.get("title") or product["name"]
        return Offer(
            key=product["id"], store="KaBuM", title=title, url=product["url"], price=float(price),
            available=bool(item.get("available")), shipping=product.get("shipping_cost"),
            seller=item.get("sellerName"),
            note="Preço no Pix; frete não calculado" if product.get("shipping_cost") is None else None,
        )
    finally:
        await page.close()


async def amazon_search(
    context: BrowserContext, query: str, filters: dict[str, list[str]] | None = None
) -> list[Offer]:
    url = "https://www.amazon.com.br/s?k=" + urllib.parse.quote_plus(query)
    page = await open_page(context, url)
    offers: list[Offer] = []
    try:
        cards = page.locator('[data-component-type="s-search-result"]')
        total = await cards.count()
        if not total:
            # A Amazon responde 503 "Algo deu errado" quando limita a origem.
            # Sem este aviso a coleta ficaria silenciosamente vazia.
            title = (await page.title()).lower()
            if "algo deu errado" in title or "errado" in title:
                raise RuntimeError("Amazon bloqueou a busca (503)")
            raise RuntimeError("Amazon nao retornou resultados")
        for index in range(total):
            card = cards.nth(index)
            title_locator = card.locator("h2")
            price_locator = card.locator(".a-price:not(.a-text-price) .a-offscreen")
            if not await title_locator.count() or not await price_locator.count():
                continue
            title = re.sub(r"\s+", " ", await title_locator.first.inner_text()).strip()
            if ignored_title(title, filters):
                continue
            asin = await card.get_attribute("data-asin") or normalize_key(title)
            price = money(await price_locator.first.text_content())
            if price is None:
                continue
            offers.append(Offer(
                key=f"amazon-{asin}", store="Amazon", title=title,
                url=f"https://www.amazon.com.br/dp/{asin}", price=price,
                source="search",
                note="Oferta descoberta; confirme vendedor e frete",
            ))
    finally:
        await page.close()
    return offers


def walk_magalu(
    node: Any, output: dict[str, Offer], filters: dict[str, list[str]] | None = None
) -> None:
    if isinstance(node, dict):
        if isinstance(node.get("offers"), list) and node.get("title") and node.get("path"):
            title = node["title"]
            if not ignored_title(title, filters):
                for raw_offer in node["offers"]:
                    best = raw_offer.get("bestPrice") or {}
                    price = best.get("totalAmount") or raw_offer.get("price")
                    seller = (raw_offer.get("seller") or {}).get("id")
                    if price:
                        key = f"magalu-{node.get('id')}-{seller}"
                        output[key] = Offer(
                            key=key, store="Magalu", title=title,
                            url="https://www.magazineluiza.com.br" + node["path"], price=float(price),
                            available=bool(node.get("available", True)), seller=seller,
                            source="search",
                            note="Preço no Pix; frete não calculado",
                        )
        for value in node.values():
            walk_magalu(value, output, filters)
    elif isinstance(node, list):
        for value in node:
            walk_magalu(value, output, filters)


async def magalu_search(
    context: BrowserContext, query: str, filters: dict[str, list[str]] | None = None
) -> list[Offer]:
    slug = query.replace(" ", "+")
    url = f"https://www.magazineluiza.com.br/busca/{slug}/?sortOrientation=asc&sortType=price"
    page = await open_page(context, url)
    try:
        raw = await page.locator("script#__NEXT_DATA__").text_content()
        output: dict[str, Offer] = {}
        walk_magalu(json.loads(raw or "{}"), output, filters)
        return list(output.values())
    finally:
        await page.close()


async def kabum_payload(context: BrowserContext, url: str) -> dict[str, Any]:
    page = await open_page(context, url)
    try:
        raw = await page.locator("script#__NEXT_DATA__").text_content()
        return json.loads(raw or "{}")
    finally:
        await page.close()


async def kabum_search(
    context: BrowserContext, query: str, filters: dict[str, list[str]] | None = None
) -> list[Offer]:
    slug = query.replace(" ", "-")
    payload = await kabum_payload(context, kabum_url(f"/busca/{slug}"))
    catalog = kabum_catalog(payload)
    if not catalog:
        target = kabum_redirect(payload)
        if target:
            catalog = kabum_catalog(await kabum_payload(context, kabum_url(target)))
    offers: list[Offer] = []
    for item in catalog:
        title = item.get("name", "")
        if ignored_title(title, filters) or not item.get("available"):
            continue
        price = (item.get("offer") or {}).get("priceWithDiscount") or item.get("priceWithDiscount")
        if not price:
            continue
        code = item.get("code")
        offers.append(Offer(
            key=f"kabum-{code}", store="KaBuM", title=title,
            url=f"https://www.kabum.com.br/produto/{code}", price=float(price),
            seller=item.get("sellerName"), source="search",
            note="Preço no Pix; frete não calculado",
        ))
    return offers


def format_brl(value: float) -> str:
    formatted = f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {formatted}"


def telegram_message(
    offer: Offer,
    limit: float | None,
    config: dict[str, Any],
    watch: Watch | None = None,
) -> str:
    project_name = html.escape(config.get("project_name", "Tchovis Promos"))
    if watch is not None:
        header = f"CONSULTA #{watch.id}"
    else:
        header = "OFERTA MONITORADA" if offer.source == "product" else "NOVA OFERTA ENCONTRADA"
    lines = [f"🧪 <b>{project_name} · {header}</b>"]
    if watch is not None:
        lines.append(f"<b>{html.escape(watch.query)}</b>")
    lines.extend([
        "",
        f"<b>{html.escape(offer.title)}</b>",
        "",
        "💰 <b>CUSTO</b>",
        f"• Produto: <b>{format_brl(offer.price)}</b>",
        f"• Frete: {format_brl(offer.shipping) if offer.shipping is not None else 'não calculado'}",
        f"• Total conhecido: <b>{format_brl(offer.final_price)}</b>",
    ])
    if limit is not None:
        lines.append(f"• Limite configurado: {format_brl(limit)}")
    lines.extend([
        "",
        "🛒 <b>COMPRA</b>",
        f"• Loja: {html.escape(offer.store)}",
        f"• Vendedor: {html.escape(offer.seller or 'não informado — conferir')}",
    ])
    if offer.coupon:
        lines.append(f"• Cupom: <code>{html.escape(offer.coupon)}</code>")
    if offer.note:
        lines.append(f"• Observação: {html.escape(offer.note)}")
    lines.extend(["", f'<a href="{html.escape(offer.url)}">Abrir oferta na loja</a>'])
    return "\n".join(lines)


def send_telegram(text: str, chat_id: str | None = None) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    target = str(chat_id or os.environ.get("TELEGRAM_CHAT_ID") or "")
    if not token or not target:
        raise RuntimeError("TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID não estão configurados em .env")
    payload = urllib.parse.urlencode({
        "chat_id": target, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "false"
    }).encode()
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=payload, method="POST"
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.loads(response.read())
    if not result.get("ok"):
        raise RuntimeError(f"Telegram recusou a mensagem: {result}")


def telegram_updates(offset: int, timeout: int = 30) -> list[dict[str, Any]]:
    """Le mensagens novas por long polling; bloqueia ate timeout segundos."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN não está configurado em .env")
    query = urllib.parse.urlencode({
        "offset": offset,
        "timeout": timeout,
        "allowed_updates": json.dumps(["message"]),
    })
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/getUpdates?{query}"
    )
    with urllib.request.urlopen(request, timeout=timeout + 15) as response:
        result = json.loads(response.read())
    if not result.get("ok"):
        raise RuntimeError(f"Telegram recusou getUpdates: {result}")
    return result.get("result", [])


def disabled_stores(config: dict[str, Any]) -> set[str]:
    """Lojas desligadas por configuracao ou por variavel de ambiente.

    A variavel TCHOVIS_DISABLED_STORES permite usar a mesma config.json em
    ambientes diferentes. No VPS a Magalu responde 403 por bloqueio de IP de
    datacenter, enquanto no notebook ela funciona.
    """
    raw = os.environ.get("TCHOVIS_DISABLED_STORES", "")
    from_env = {item.strip().lower() for item in raw.split(",") if item.strip()}
    from_config = {str(item).lower() for item in config.get("disabled_stores", [])}
    return from_env | from_config


def entry_enabled(entry: dict[str, Any], blocked: set[str]) -> bool:
    if entry.get("enabled") is False:
        return False
    return str(entry.get("store", "")).lower() not in blocked


async def collect(
    config: dict[str, Any], extra_searches: list[dict[str, Any]] | None = None
) -> tuple[list[Offer], list[str]]:
    blocked = disabled_stores(config)
    products = [p for p in config.get("products", []) if entry_enabled(p, blocked)]
    searches = [s for s in config.get("searches", []) if entry_enabled(s, blocked)]
    searches += [s for s in (extra_searches or []) if entry_enabled(s, blocked)]
    if blocked:
        print(f"Lojas desligadas: {', '.join(sorted(blocked))}")

    http_products = [p for p in products if transport_for(p) == "http"]
    http_searches = [s for s in searches if transport_for(s) == "http"]
    web_products = [p for p in products if transport_for(p) != "http"]
    web_searches = [s for s in searches if transport_for(s) != "http"]
    print(
        f"Alvos: {len(products)} produtos, {len(searches)} buscas "
        f"(HTTP: {len(http_products)}+{len(http_searches)}, "
        f"navegador: {len(web_products)}+{len(web_searches)})"
    )

    offers, errors = collect_http(http_products, http_searches, config)

    if not web_products and not web_searches:
        print("Nenhuma fonte exige navegador; Playwright nao foi iniciado.")
        return list(offers.values()), errors

    async with async_playwright() as playwright:
        browser = await playwright.firefox.launch(headless=True)
        context = await browser.new_context(locale="pt-BR", viewport={"width": 1366, "height": 900})
        for product in web_products:
            last_error: Exception | None = None
            for attempt in range(2):
                try:
                    store = product["store"]
                    if store == "amazon":
                        offer = await amazon_product(context, product)
                    elif store == "magalu":
                        offer = await magalu_product(context, product)
                    elif store == "kabum":
                        offer = await kabum_product(context, product)
                    else:
                        raise ValueError(f"Loja não suportada: {store}")
                    offers[offer_slot(offer)] = offer
                    last_error = None
                    break
                except Exception as error:  # noqa: BLE001
                    last_error = error
                    if attempt == 0:
                        await asyncio.sleep(2)
            if last_error is not None:
                errors.append(f"{product.get('id')}: {last_error}")
        for search in web_searches:
            last_error: Exception | None = None
            for attempt in range(2):
                try:
                    store = str(search["store"]).lower()
                    filters = title_filters(search, config)
                    if store == "amazon":
                        found = await amazon_search(context, search["query"], filters)
                    elif store == "magalu":
                        found = await magalu_search(context, search["query"], filters)
                    elif store == "kabum":
                        found = await kabum_search(context, search["query"], filters)
                    else:
                        found = []
                    for offer in found:
                        offer.watch_id = search.get("watch_id")
                        offers.setdefault(offer_slot(offer), offer)
                    last_error = None
                    break
                except Exception as error:  # noqa: BLE001
                    last_error = error
                    if attempt == 0:
                        await asyncio.sleep(5)
            if last_error is not None:
                errors.append(f"busca {search.get('query')} em {search.get('store')}: {last_error}")
            # Pausa curta entre buscas no navegador para reduzir a chance de
            # acionar limites de taxa das lojas.
            await asyncio.sleep(float(config.get("browser_delay_seconds", 3)))
        await browser.close()
    return list(offers.values()), errors


# --- ciclo de coleta ---------------------------------------------------


def offer_limit(
    offer: Offer, config: dict[str, Any], watch: Watch | None
) -> tuple[bool, float | None, bool]:
    """Decide se a oferta interessa.

    Devolve (interessa, limite, alertar_de_primeira). Uma consulta do bot
    usa o preco que o usuario pediu; sem preco, nao existe "atingiu o
    alvo" e o alerta fica para quando o valor cair. Um produto fixo do
    config.json usa o maximum_price da propria entrada.
    """
    if not offer.available:
        return False, None, True
    if watch is not None:
        if watch.maximum_price is None:
            return True, None, False
        return offer.final_price <= watch.maximum_price, watch.maximum_price, True
    limit = next(
        (
            float(item["maximum_price"])
            for item in config.get("products", [])
            if item["id"] == offer.key and item.get("maximum_price") is not None
        ),
        None,
    )
    if limit is None:
        return False, None, True
    return offer.final_price <= limit, limit, True


async def cycle(
    config: dict[str, Any], history: History, dry_run: bool = False
) -> tuple[int, int, list[str]]:
    """Uma rodada completa: coleta, grava o historico e envia os alertas."""
    watches = history.watches()
    by_id = {watch.id: watch for watch in watches}
    extra = [entry for watch in watches for entry in watch.as_searches()]
    if watches:
        print(f"Consultas monitoradas ativas: {len(watches)}")

    offers, errors = await collect(config, extra)

    # A mesma URL pode vir de uma busca e de um produto fixo; o produto
    # ganha, porque traz frete e cupom. A deduplicacao e por consulta, para
    # que duas consultas diferentes nao se anulem.
    deduplicated: dict[tuple[int, str], Offer] = {}
    for offer in sorted(offers, key=lambda item: item.source == "search"):
        deduplicated.setdefault(
            (offer.watch_id or 0, offer.url.split("?")[0].rstrip("/")), offer
        )
    offers = list(deduplicated.values())

    minimum_drop = float(config.get("minimum_price_drop", 50))
    candidates: list[tuple[Offer, float | None, Watch | None, bool]] = []
    for offer in offers:
        history.record(offer)
        watch = by_id.get(offer.watch_id) if offer.watch_id else None
        interesting, limit, alert_on_first = offer_limit(offer, config, watch)
        if interesting:
            candidates.append((offer, limit, watch, alert_on_first))

    candidates.sort(key=lambda item: item[0].final_price)
    within = [item for item in candidates if item[1] is not None]
    print(f"Consultadas {len(offers)} ofertas; {len(within)} dentro do limite.")

    sent = 0
    for offer, limit, watch, alert_on_first in candidates:
        if limit is not None:
            print(f"- {offer.store}: {format_brl(offer.final_price)} | {offer.title} | {offer.url}")
        if dry_run:
            continue
        if not history.should_alert(offer, minimum_drop, alert_on_first):
            continue
        if limit is None:
            print(f"- queda em {offer.store}: {format_brl(offer.final_price)} | {offer.title}")
        send_telegram(
            telegram_message(offer, limit, config, watch),
            watch.chat_id if watch else None,
        )
        history.mark_alerted(offer)
        sent += 1
        print("  alerta enviado")
    return len(offers), sent, errors


# --- comandos do Telegram ----------------------------------------------

SEARCHABLE_STORES = ("kabum", "amazon", "magalu")

HELP_TEXT = (
    "<b>Comandos</b>\n"
    "/monitorar &lt;termo&gt; [ate &lt;preço&gt;] [em &lt;lojas&gt;]\n"
    "/lista — consultas cadastradas\n"
    "/remover &lt;id&gt;\n"
    "/pausar &lt;id&gt; · /ativar &lt;id&gt;\n"
    "/agora — roda uma coleta imediatamente\n"
    "/lojas — lojas que aceitam busca por termo\n\n"
    "<b>Exemplos</b>\n"
    "<code>/monitorar air fryer 5 litros ate 450 em kabum,magalu</code>\n"
    "<code>/monitorar monitor 27 144hz em kabum</code>\n"
    "<code>/monitorar echo dot</code>\n\n"
    "Sem preço o alerta vem quando o valor cair. Sem lojas, usa todas as "
    "disponíveis."
)


def parse_price(raw: str) -> float | None:
    """Le preco em formato brasileiro: 450, R$ 1.299,90, 1.299."""
    cleaned = re.sub(r"[^\d,.]", "", raw)
    if not cleaned:
        return None
    if "," in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif cleaned.count(".") == 1 and len(cleaned.split(".")[1]) == 3:
        # 1.299 e milhar, nao decimal.
        cleaned = cleaned.replace(".", "")
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return value if value > 0 else None


def parse_watch_command(
    text: str, config: dict[str, Any]
) -> tuple[str, float | None, list[str], str | None]:
    """Extrai termo, preco e lojas de /monitorar.

    As lojas so sao reconhecidas quando todos os nomes depois de "em" sao
    lojas conhecidas; caso contrario o trecho continua fazendo parte do
    termo, para nao estragar uma busca como "suporte em L".
    """
    body = re.sub(r"^/\w+(?:@\w+)?\s*", "", text.strip())
    stores: list[str] = []
    price: float | None = None

    for _ in range(2):
        tail = re.search(r"\s+em\s+([A-Za-z0-9,\s]+)$", body, re.I)
        if tail and not stores:
            tokens = [item for item in re.split(r"[,\s]+", tail.group(1).lower()) if item]
            if tokens and all(token in SEARCHABLE_STORES for token in tokens):
                stores = list(dict.fromkeys(tokens))
                body = body[: tail.start()].strip()
        tail = re.search(
            r"\s+(?:ate|até|por|max|maximo|máximo)\s+(R?\$?\s*[\d.,]+)$", body, re.I
        )
        if tail and price is None:
            price = parse_price(tail.group(1))
            if price is not None:
                body = body[: tail.start()].strip()

    if not body:
        return "", None, [], (
            "Diga o que monitorar. Exemplo:\n"
            "<code>/monitorar air fryer 5 litros ate 450 em kabum</code>"
        )

    blocked = disabled_stores(config)
    if not stores:
        stores = [store for store in SEARCHABLE_STORES if store not in blocked]
    usable = [store for store in stores if store not in blocked]
    if not usable:
        return "", None, [], (
            "Todas as lojas pedidas estão desligadas neste ambiente "
            f"({', '.join(sorted(blocked))}). Veja /lojas."
        )
    return body, price, usable, None


def command_argument(text: str) -> str:
    parts = text.strip().split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


async def first_look(
    watch: Watch, config: dict[str, Any], history: History, top: int = 5
) -> str:
    """Pesquisa a consulta recem-criada e resume o que existe agora.

    Tudo que entra nesse resumo e marcado como avisado, senao o proximo
    ciclo repetiria a lista inteira como se fosse novidade.
    """
    isolated = {**config, "products": [], "searches": []}
    offers, errors = await collect(isolated, watch.as_searches())
    for offer in offers:
        history.record(offer)

    matching = [
        offer for offer in offers
        if offer.available
        and (watch.maximum_price is None or offer.final_price <= watch.maximum_price)
    ]
    matching.sort(key=lambda offer: offer.final_price)
    for offer in matching:
        history.mark_alerted(offer)

    if not matching:
        reply = [
            f"Nenhuma oferta encontrada agora para <b>#{watch.id}</b> "
            f"{html.escape(watch.query)}."
        ]
        if watch.maximum_price is not None:
            reply.append(
                f"Consultei {len(offers)} itens, nenhum até "
                f"{format_brl(watch.maximum_price)}."
            )
        reply.append("Deixo monitorando e aviso quando aparecer.")
    else:
        reply = [f"<b>Encontrei agora ({len(matching)} de {len(offers)} itens):</b>", ""]
        for offer in matching[:top]:
            reply.append(
                f"• <b>{format_brl(offer.final_price)}</b> — {offer.store}\n"
                f'  <a href="{html.escape(offer.url)}">{html.escape(offer.title[:90])}</a>'
            )
        if len(matching) > top:
            reply.append(f"\n… e outras {len(matching) - top}.")
        reply.append("\nA partir daqui aviso quando o preço cair.")
    if errors:
        reply.append(f"\n⚠️ {len(errors)} fonte(s) falharam nesta busca.")
    return "\n".join(reply)


async def handle_command(
    text: str, chat_id: str, config: dict[str, Any], history: History
) -> str | None:
    """Executa um comando e devolve a resposta a ser enviada."""
    if not text.startswith("/"):
        return (
            "Mando alertas de preço. Use "
            "<code>/monitorar &lt;termo&gt; ate &lt;preço&gt; em &lt;lojas&gt;</code> "
            "ou /ajuda."
        )
    command = text.strip().split()[0].lower().lstrip("/").split("@")[0]

    if command in ("start", "ajuda", "help"):
        return HELP_TEXT

    if command == "lojas":
        blocked = disabled_stores(config)
        lines = ["<b>Lojas para busca por termo</b>"]
        for store in SEARCHABLE_STORES:
            state = "desligada neste ambiente" if store in blocked else "disponível"
            lines.append(f"• {store}: {state}")
        lines.append("\nA busca por termo cobre essas lojas. Produtos avulsos podem ser")
        lines.append("acompanhados pela lista products do config.json.")
        return "\n".join(lines)

    if command in ("lista", "consultas"):
        items = history.watches(active_only=False)
        if not items:
            return "Nenhuma consulta cadastrada. Use /monitorar."
        return "<b>Consultas</b>\n\n" + "\n\n".join(item.describe() for item in items)

    if command in ("remover", "apagar"):
        argument = command_argument(text)
        if not argument.isdigit():
            return "Use /remover &lt;id&gt;, como em <code>/remover 3</code>. Veja /lista."
        if history.remove_watch(int(argument)):
            return f"Consulta #{argument} removida."
        return f"Não achei a consulta #{argument}."

    if command in ("pausar", "ativar"):
        argument = command_argument(text)
        if not argument.isdigit():
            return f"Use /{command} &lt;id&gt;. Veja /lista."
        active = command == "ativar"
        if history.set_watch_active(int(argument), active):
            return f"Consulta #{argument} {'ativada' if active else 'pausada'}."
        return f"Não achei a consulta #{argument}."

    if command in ("agora", "coletar"):
        send_telegram("🔄 Rodando uma coleta agora...", chat_id)
        total, sent, errors = await cycle(config, history)
        reply = f"Coleta concluída: {total} ofertas, {sent} alerta(s)."
        if errors:
            reply += f"\n⚠️ {len(errors)} fonte(s) falharam."
        return reply

    if command in ("monitorar", "monitore", "buscar", "watch"):
        query, price, stores, error = parse_watch_command(text, config)
        if error:
            return error
        watch = history.add_watch(chat_id, query, stores, price)
        limit = format_brl(price) if price else "sem limite; aviso quando cair"
        send_telegram(
            f"✅ Consulta <b>#{watch.id}</b> salva.\n"
            f"Termo: {html.escape(query)}\n"
            f"Lojas: {', '.join(stores)}\n"
            f"Limite: {limit}\n\n"
            "Pesquisando agora...",
            chat_id,
        )
        return await first_look(watch, config, history)

    return f"Não conheço /{html.escape(command)}. Use /ajuda."


async def serve(config: dict[str, Any]) -> int:
    """Processo residente: escuta o bot e coleta no intervalo configurado."""
    allowed = str(os.environ.get("TELEGRAM_CHAT_ID", "")).strip()
    if not allowed:
        print(
            "TELEGRAM_CHAT_ID não está configurado; sem ele o bot não sabe de "
            "quem aceitar comandos.",
            file=sys.stderr,
        )
        return 1

    history = History(DB_PATH)
    interval = float(config.get("check_interval_minutes", 30)) * 60
    offset = int(history.state_get("update_offset") or 0)
    last_cycle = float(history.state_get("last_cycle") or 0)
    print(f"Bot ouvindo. Coleta a cada {interval / 60:.0f} min. Chat autorizado: {allowed}")
    send_telegram("🟢 Monitor ligado. /ajuda mostra os comandos.", allowed)

    while True:
        try:
            updates = await asyncio.to_thread(telegram_updates, offset, 30)
        except Exception as error:  # noqa: BLE001
            print(f"getUpdates falhou: {error}", file=sys.stderr)
            await asyncio.sleep(15)
            updates = []

        for update in updates:
            offset = int(update["update_id"]) + 1
            history.state_set("update_offset", str(offset))
            message = update.get("message") or {}
            text = str(message.get("text") or "").strip()
            sender = str((message.get("chat") or {}).get("id", ""))
            if not text:
                continue
            if sender != allowed:
                # Silencio com desconhecidos: quem achasse o bot poderia
                # cadastrar consultas e ler os resultados.
                print(f"Mensagem ignorada de chat não autorizado: {sender}", file=sys.stderr)
                continue
            print(f"Comando: {text}")
            try:
                reply = await handle_command(text, sender, config, history)
            except Exception as error:  # noqa: BLE001
                reply = f"Falhou: {html.escape(str(error))}"
                print(f"Comando falhou: {error}", file=sys.stderr)
            if reply:
                send_telegram(reply, sender)

        if time.time() - last_cycle >= interval:
            last_cycle = time.time()
            history.state_set("last_cycle", str(last_cycle))
            try:
                total, sent, errors = await cycle(config, history)
                print(f"Ciclo: {total} ofertas, {sent} alerta(s), {len(errors)} erro(s)")
                for error in errors:
                    print(f"- {error}", file=sys.stderr)
            except Exception as error:  # noqa: BLE001
                print(f"Ciclo falhou: {error}", file=sys.stderr)


async def run(args: argparse.Namespace) -> int:
    config_path = Path(args.config).resolve()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    load_env(ROOT / ".env")
    if args.test_telegram:
        send_telegram("✅ Tchovis Promos configurado corretamente.")
        print("Mensagem de teste enviada.")
        return 0
    if args.serve:
        return await serve(config)

    history = History(DB_PATH)
    total, sent, errors = await cycle(config, history, args.dry_run)
    if errors:
        print("Erros parciais:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
    return 0 if total else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(ROOT / "config.json"))
    parser.add_argument("--dry-run", action="store_true", help="Consulta e grava histórico sem enviar Telegram")
    parser.add_argument("--test-telegram", action="store_true", help="Envia somente uma mensagem de teste")
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Fica ouvindo os comandos do bot e coletando no intervalo configurado",
    )
    return parser.parse_args()


def main() -> None:
    raise SystemExit(asyncio.run(run(parse_args())))


if __name__ == "__main__":
    main()
