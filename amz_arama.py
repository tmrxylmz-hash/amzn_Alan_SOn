import html
import re
import sys
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus, urlparse
from urllib.request import Request, urlopen

from PyQt6.QtCore import QSettings, Qt
from PyQt6.QtWidgets import (
    QApplication,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QDoubleSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


@dataclass
class Product:
    asin: str
    title: str = ""
    price: float | None = None
    rating: float | None = None
    seller_count: int | None = None
    sales_rank: int | None = None
    seller_type: str = ""
    image_url: str = ""
    product_url: str = ""
    brand: str = ""


ASIN_PATTERN = re.compile(r"\b[A-Z0-9]{10}\b", re.IGNORECASE)


def resolve_input(value: str) -> tuple[str, str]:
    value = value.strip()
    if ASIN_PATTERN.fullmatch(value):
        return value.upper(), f"https://www.amazon.de/dp/{value.upper()}"

    parsed = urlparse(value)
    if parsed.scheme in {"http", "https"} and "amazon." in parsed.netloc.lower():
        match = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", value, re.IGNORECASE)
        return (match.group(1).upper(), value) if match else ("", value)

    return "", ""


def resolve_search_url(value: str) -> tuple[str, str]:
    """Convert an ASIN, Amazon URL, or category text into a search URL."""
    asin, url = resolve_input(value)
    if url:
        if asin:
            domain = urlparse(url).netloc.split(".")[-1] or "de"
            return asin, f"https://www.amazon.{domain}/s?k={quote_plus(asin)}"
        return asin, url
    domain = "com" if value.lower().startswith("amazon.com") else "de"
    return "", f"https://www.amazon.{domain}/s?k={quote_plus(value.strip())}"


def parse_number(value: str) -> float | None:
    cleaned = re.sub(r"[^\d,.]", "", value or "")
    if not cleaned:
        return None
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    else:
        cleaned = cleaned.replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_saved_html(source: str, domain: str) -> list[Product]:
    """Parse Amazon product cards without relying on balanced HTML tags."""
    products: list[Product] = []
    card_starts = list(re.finditer(
        r'<[^>]*\bdata-asin=["\']([A-Z0-9]{10})["\'][^>]*>',
        source,
        re.IGNORECASE,
    ))
    for index, match in enumerate(card_starts):
        card_end = card_starts[index + 1].start() if index + 1 < len(card_starts) else min(
            len(source), match.end() + 20000
        )
        asin = match.group(1)
        card = source[match.start():card_end]
        title_match = re.search(
            r'class=["\'][^"\']*(?:a-text-normal|a-size-base-plus|product-title)[^"\']*["\'][^>]*>(.*?)<',
            card,
            re.IGNORECASE | re.DOTALL,
        )
        if not title_match:
            title_match = re.search(
                r'(?:aria-label|alt)=["\']([^"\']{5,})["\']',
                card,
                re.IGNORECASE,
            )
        price_match = re.search(r'class=["\'][^"\']*a-offscreen[^"\']*["\'][^>]*>(.*?)<', card, re.I | re.S)
        rating_match = re.search(r'([0-5][,.]\d)\s*(?:out of|von|star|Stern)', html.unescape(card), re.I)
        image_match = re.search(r'<img[^>]+(?:src|data-image-src)=["\']([^"\']+)["\']', card, re.I)
        brand_match = re.search(
            r'(?:brand|bylineInfo)[^>]*>(.*?)<|(?:brand|manufacturer)["\']?\s*[:=]\s*["\']([^"\']+)',
            card,
            re.I | re.S,
        )
        title = re.sub(r"\s+", " ", html.unescape(title_match.group(1))).strip() if title_match else ""
        price = parse_number(html.unescape(price_match.group(1))) if price_match else None
        rating = parse_number(rating_match.group(1)) if rating_match else None
        image_url = html.unescape(image_match.group(1)) if image_match else ""
        brand = next((re.sub(r"\s+", " ", html.unescape(value)).strip()
                      for value in (brand_match.groups() if brand_match else ()) if value), "")
        products.append(Product(
            asin=asin.upper(),
            title=title,
            price=price,
            rating=rating,
            image_url=image_url,
            product_url=f"https://www.amazon.{domain}/dp/{asin.upper()}",
            brand=brand,
        ))
    unique: dict[str, Product] = {}
    for product in products:
        unique.setdefault(product.asin, product)
    return list(unique.values())


def _title_tokens(title: str) -> set[str]:
    stop_words = {
        "the", "and", "with", "for", "amazon", "original", "new",
        "der", "die", "das", "und", "mit", "für", "von",
    }
    return {
        token for token in re.findall(r"[a-z0-9]+", title.lower())
        if len(token) > 2 and token not in stop_words
    }


def select_similar_products(products: list[Product], anchor: Product | None, limit: int) -> list[Product]:
    """Exclude the anchor's brand/model family and obvious size/color variants."""
    anchor_tokens = _title_tokens(anchor.title) if anchor else set()
    anchor_brand = anchor.brand.lower().strip() if anchor else ""
    variation_words = {
        "black", "white", "blue", "red", "green", "small", "medium", "large",
        "xl", "s", "m", "l", "schwarz", "weiß", "blau", "rot", "klein", "groß",
        "version", "variante", "pack", "set",
    }
    selected: list[Product] = []
    seen: set[str] = set()
    for product in products:
        if product.asin in seen or (anchor and product.asin == anchor.asin):
            continue
        seen.add(product.asin)
        product_brand = product.brand.lower().strip()
        if anchor_brand and product_brand and product_brand == anchor_brand:
            continue
        product_tokens = _title_tokens(product.title)
        shared = anchor_tokens & product_tokens
        overlap = len(shared) / len(anchor_tokens) if anchor_tokens else 0
        if len(shared) >= 4 and overlap >= 0.7:
            continue
        if anchor_tokens and (product_tokens - anchor_tokens) & variation_words and len(shared) >= 2:
            continue
        selected.append(product)
        if len(selected) >= limit:
            break
    return selected


def fetch_amazon_page(url: str) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/131.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
        },
    )
    with urlopen(request, timeout=20) as response:
        return response.read().decode("utf-8", errors="replace")


def extract_product_title(source: str) -> str:
    title_patterns = (
        r'<span[^>]+id=["\']productTitle["\'][^>]*>(.*?)<',
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)',
        r'<h1[^>]*>(.*?)<',
        r'<title[^>]*>(.*?)<',
    )
    for pattern in title_patterns:
        match = re.search(pattern, source, re.IGNORECASE | re.DOTALL)
        if match:
            title = re.sub(r"\s+", " ", html.unescape(match.group(1))).strip()
            title = re.sub(r"\s*:\s*Amazon\.[a-z.]+.*$", "", title, flags=re.IGNORECASE)
            if len(title) >= 4 and title.lower() not in {"amazon", "amazon.de", "amazon.com"}:
                return title
    return ""


def is_access_blocked(source: str) -> bool:
    lowered = source.lower()
    markers = (
        "captcha", "robot check", "ein mensch", "automated access",
        "sorry, something went wrong", "sorry! something went wrong",
        "503 service unavailable", "to discuss automated access",
    )
    return len(source) < 20000 or any(marker in lowered for marker in markers)


class AmzAramaWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.products: list[Product] = []
        self.settings = QSettings("AMZ_ARAMA", "AmazonProductResearch")
        self.setWindowTitle("AMZ_ARAMA - Amazon Ürün Araştırma")
        self.resize(1100, 680)
        self.build_ui()

    def build_ui(self):
        root = QVBoxLayout(self)

        source_box = QGroupBox("Arama kaynağı")
        source_layout = QHBoxLayout(source_box)
        self.source_edit = QLineEdit()
        self.source_edit.setPlaceholderText("Amazon linki, ASIN veya kategori linki girin")
        source_layout.addWidget(self.source_edit, 1)

        self.start_button = QPushButton("Aramayı Başlat")
        self.start_button.clicked.connect(self.start_search)
        source_layout.addWidget(self.start_button)
        root.addWidget(source_box)

        filter_box = QGroupBox("Filtreler")
        filter_layout = QGridLayout(filter_box)
        self.min_price = QDoubleSpinBox()
        self.min_price.setRange(0, 999999)
        self.min_price.setPrefix("€ ")
        self.max_price = QDoubleSpinBox()
        self.max_price.setRange(0, 999999)
        self.max_price.setValue(999999)
        self.max_price.setPrefix("€ ")
        self.min_sellers = QSpinBox()
        self.min_sellers.setRange(0, 99999)
        self.max_sellers = QSpinBox()
        self.max_sellers.setRange(0, 99999)
        self.max_sellers.setValue(99999)
        self.min_rank = QSpinBox()
        self.min_rank.setRange(0, 99999999)
        self.max_rank = QSpinBox()
        self.max_rank.setRange(0, 99999999)
        self.max_rank.setValue(99999999)
        self.result_count = QSpinBox()
        self.result_count.setRange(1, 500)
        self.result_count.setValue(20)
        self.source_edit.setText(self.settings.value("query", "", str))
        for key, widget in (
            ("min_price", self.min_price), ("max_price", self.max_price),
            ("min_sellers", self.min_sellers), ("max_sellers", self.max_sellers),
            ("min_rank", self.min_rank), ("max_rank", self.max_rank),
            ("result_count", self.result_count),
        ):
            saved_value = self.settings.value(key)
            if saved_value is not None:
                widget.setValue(float(saved_value) if isinstance(widget, QDoubleSpinBox) else int(saved_value))

        fields = [
            ("Min. fiyat", self.min_price), ("Max. fiyat", self.max_price),
            ("Min. satıcı", self.min_sellers), ("Max. satıcı", self.max_sellers),
            ("Min. Sales Rank", self.min_rank), ("Max. Sales Rank", self.max_rank),
            ("Benzer ürün adedi", self.result_count),
        ]
        for index, (label, widget) in enumerate(fields):
            filter_layout.addWidget(QLabel(label), index // 4 * 2, index % 4)
            filter_layout.addWidget(widget, index // 4 * 2 + 1, index % 4)
            if hasattr(widget, "valueChanged"):
                widget.valueChanged.connect(self.render_results)
                widget.valueChanged.connect(lambda value, key=fields[index][0]: self.save_setting(key, value))
        root.addWidget(filter_box)

        self.status_label = QLabel(
            "Amazon linki, ASIN veya kategori girin ve Aramayı Başlat'a basın."
        )
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)

        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            ["ASIN", "Ürün adı", "Fiyat", "Puan", "Satıcı", "Sales Rank", "Satıcı tipi", "Link"]
        )
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setAlternatingRowColors(True)
        root.addWidget(self.table, 1)

    def start_search(self):
        query = self.source_edit.text().strip()
        if not query:
            QMessageBox.warning(self, "Eksik bilgi", "Önce Amazon linki, ASIN veya kategori girin.")
            return
        self.settings.setValue("query", query)

        asin, url = resolve_search_url(query)
        self.start_button.setEnabled(False)
        self.status_label.setText("Amazon aranıyor, lütfen bekleyin...")
        QApplication.processEvents()
        try:
            page_source = fetch_amazon_page(url)
            if is_access_blocked(page_source):
                raise RuntimeError(
                    "Amazon ürün sonuçları yerine erişim doğrulama sayfası döndürdü."
                )
            parsed_url = urlparse(url)
            domain = parsed_url.netloc.split(".")[-1] or "de"
            products = parse_saved_html(page_source, domain)

            anchor = next((item for item in products if item.asin == asin), None)
            if asin and not anchor:
                anchor = Product(asin=asin, product_url=url)
            if asin:
                if not anchor:
                    anchor = Product(asin=asin, product_url=url)
                anchor.title = anchor.title or extract_product_title(page_source)
                search_terms = anchor.title or asin
                similar_url = f"https://www.amazon.{domain}/s?k={quote_plus(search_terms)}"
                similar_source = fetch_amazon_page(similar_url)
                if is_access_blocked(similar_source):
                    raise RuntimeError(
                        "Amazon benzer ürün aramasını doğrulama nedeniyle engelledi."
                    )
                products = parse_saved_html(similar_source, domain)
            self.products = select_similar_products(
                products, anchor, self.result_count.value()
            )
        except (HTTPError, URLError, TimeoutError, OSError, RuntimeError) as error:
            self.start_button.setEnabled(True)
            QMessageBox.critical(
                self,
                "Amazon erişim hatası",
                f"Amazon sayfası alınamadı:\n{error}\n\n"
                "Amazon erişimi engellediyse tarayıcıdan manuel açıp tekrar deneyin.",
            )
            return

        self.render_results()
        self.start_button.setEnabled(True)
        if self.products:
            self.status_label.setText(
                f"{len(self.products)} benzer ürün bulundu; aynı marka ve varyasyonlar elendi."
            )
        else:
            self.status_label.setText(
                "Ürün bulunamadı. Amazon arama sayfası CAPTCHA veya erişim engeli döndürmüş olabilir."
            )

    def render_results(self, *_):
        def in_range(product: Product) -> bool:
            price_ok = product.price is None or self.min_price.value() <= product.price <= self.max_price.value()
            sellers_ok = product.seller_count is None or self.min_sellers.value() <= product.seller_count <= self.max_sellers.value()
            rank_ok = product.sales_rank is None or self.min_rank.value() <= product.sales_rank <= self.max_rank.value()
            return price_ok and sellers_ok and rank_ok

        visible = [product for product in self.products if in_range(product)][: self.result_count.value()]
        self.table.setRowCount(len(visible))
        for row, product in enumerate(visible):
            values = [
                product.asin,
                product.title or "Başlık bulunamadı",
                f"€{product.price:.2f}" if product.price is not None else "Yok",
                f"{product.rating:.1f}" if product.rating is not None else "Yok",
                str(product.seller_count) if product.seller_count is not None else "Yok",
                str(product.sales_rank) if product.sales_rank is not None else "Yok",
                product.seller_type or "Yok",
                product.product_url,
            ]
            for column, value in enumerate(values):
                self.table.setItem(row, column, QTableWidgetItem(value))

    def save_setting(self, label: str, value: float):
        setting_keys = {
            "Min. fiyat": "min_price", "Max. fiyat": "max_price",
            "Min. satıcı": "min_sellers", "Max. satıcı": "max_sellers",
            "Min. Sales Rank": "min_rank", "Max. Sales Rank": "max_rank",
            "Benzer ürün adedi": "result_count",
        }
        key = setting_keys.get(label)
        if key:
            self.settings.setValue(key, value)


def main():
    app = QApplication(sys.argv)
    window = AmzAramaWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
