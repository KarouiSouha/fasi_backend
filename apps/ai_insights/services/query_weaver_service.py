import re
from datetime import date, timedelta

from django.utils.timezone import now


class QueryWeaverService:
    MONTHS = {
        "january": 1, "janvier": 1, "février": 2, "fevrier": 2, "february": 2,
        "mars": 3, "march": 3, "avril": 4, "april": 4,
        "mai": 5, "may": 5, "juin": 6, "june": 6,
        "juillet": 7, "july": 7, "août": 8, "aout": 8, "august": 8,
        "septembre": 9, "september": 9, "octobre": 10, "october": 10,
        "novembre": 11, "november": 11, "décembre": 12, "decembre": 12, "december": 12,
    }
    # Keep numbers in tokenization for product matching
    PRODUCT_TOKEN_PATTERN = re.compile(r"[\w\u0600-\u06FF]+", re.UNICODE)

    DATE_PATTERN = re.compile(r"(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4})")
    YEAR_PATTERN = re.compile(r"(20\d{2})")

    def parse_product_name(self, text, company=None):
        text_lower = text.lower()

        search = re.search(r"(?:product|produit|item|article)\s+(?:named\s+)?([\w\s\-\u0600-\u06FF]+)", text, re.IGNORECASE)
        if search:
            candidate = search.group(1).strip()
            return candidate[:120]

        search = re.search(r"(?:pour|de|du|des|avec)\s+([\w\s\-\u0600-\u06FF]+?)(?:\s+pour|\s+de|\s+du|\s+des|\s+avec|\s+dans|\s+en|$)", text, re.IGNORECASE)
        if search:
            candidate = search.group(1).strip()
            if len(candidate) > 3:
                return candidate[:120]

        if company:
            matched = self._match_product_name_from_db(text_lower, company)
            if matched:
                return matched

        return ""

    def parse_product_names(self, text, company=None):
        text_lower = (text or "").lower().strip()

        # Search for explicit product names from the database first.
        if company:
            matched_products = self._match_product_names_from_db(text_lower, company)
            if matched_products:
                return matched_products

            # If a phrase is explicitly provided after a keyword, try that phrase against the DB.
            phrase = self._extract_named_product_phrase(text_lower)
            if phrase:
                matched_products = self._match_product_names_from_db(phrase, company)
                if matched_products:
                    return matched_products

            # Try a relaxed token-based search against DB product names.
            matched_products = self._match_product_names_from_db_by_terms(text_lower, company)
            if matched_products:
                return matched_products

        return []

    def _match_product_names_from_db(self, text_lower, company):
        try:
            from apps.transactions.models import MaterialMovement
            names = (
                MaterialMovement.objects.filter(company=company)
                .exclude(material_name__isnull=True)
                .exclude(material_name__exact="")
                .values_list("material_name", flat=True)
                .distinct()[:400]
            )
        except Exception:
            return []

        query_tokens = set(self._tokenize(text_lower))
        matched = []
        
        for name in names:
            lowered = name.lower()
            name_tokens = set(self._tokenize(lowered))
            
            if not name_tokens:
                continue
            
            # Exact substring match (highest priority)
            if lowered in text_lower:
                if name.strip() not in matched:
                    matched.append(name.strip())
                continue
            
            # Token-based matching: require significant overlap
            common = len(query_tokens & name_tokens)
            total_name_tokens = len(name_tokens)
            
            # At least 50% of product name tokens must be in the query
            if common >= 2 and common >= total_name_tokens * 0.5:
                if name.strip() not in matched:
                    matched.append(name.strip())
        
        return matched

    def _match_product_names_from_db_by_terms(self, text_lower, company):
        """Relaxed token-based product matching (fallback when exact match fails)"""
        try:
            from apps.transactions.models import MaterialMovement
            names = (
                MaterialMovement.objects.filter(company=company)
                .exclude(material_name__isnull=True)
                .exclude(material_name__exact="")
                .values_list("material_name", flat=True)
                .distinct()[:400]
            )
        except Exception:
            return []

        query_tokens = set(self._tokenize(text_lower))
        matched = []
        for name in names:
            name_tokens = set(self._tokenize(name.lower()))
            if not name_tokens:
                continue
            common = len(query_tokens & name_tokens)
            # Lower threshold for token-based matching: 2 common tokens
            if common >= 2 and name.strip() not in matched:
                matched.append(name.strip())
        return matched
    
    def _extract_named_product_phrase(self, text_lower):
        """Extract product name from keyword patterns (not required for detection)"""
        patterns = [
            r"(?:what about|about)\s+(.+?)(?:\?|$)",
            r"(?:product|produit)?\s*(?:named|nommé)\s+(.+?)(?:\?|$)",
            r"(?:for|pour)\s+(?:product|produit)?\s*(.+?)(?:\?|$)",
        ]
        for pattern in patterns:
            match = re.search(pattern, text_lower)
            if match:
                phrase = match.group(1).strip()
                phrase = re.sub(r"[?.!]+$", "", phrase).strip()
                if len(phrase) > 2:
                    return phrase
        return ""

    def _tokenize(self, text):
        # Include all tokens, even single characters (numbers, letters)
        # This helps with product names like "2 MP Camera"
        return [
            token for token in self.PRODUCT_TOKEN_PATTERN.findall(text.lower())
            if len(token) >= 1  # Keep even single-char tokens
        ]

    def parse_date_range(self, text):
        text_lower = (text or "").lower()
        dates = self.DATE_PATTERN.findall(text_lower)
        if len(dates) >= 2:
            start = self._parse_date(dates[0])
            end = self._parse_date(dates[1])
            if start and end:
                return start, end

        if "today" in text_lower or "aujourd'hui" in text_lower or "todays" in text_lower:
            end = date.today()
        else:
            end = None

        for token, month in self.MONTHS.items():
            if token in text_lower:
                year = self._extract_year(text_lower) or date.today().year
                start = date(year, month, 1)
                end = date(year, month, 28)
                return start, self._end_of_month(start)

        if "january" in text_lower or "janvier" in text_lower:
            year = self._extract_year(text_lower) or date.today().year
            return date(year, 1, 1), date(year, 1, 31)

        if "this month" in text_lower or "ce mois" in text_lower:
            today = date.today()
            return date(today.year, today.month, 1), today

        if "year to date" in text_lower or "ytd" in text_lower:
            today = date.today()
            return date(today.year, 1, 1), today

        return None, None

    def _parse_date(self, raw):
        parts = re.split(r"[\/\-]", raw)
        if len(parts) != 3:
            return None
        day, month, year = parts
        day = int(day)
        month = int(month)
        year = int(year)
        if year < 100:
            year += 2000
        try:
            return date(year, month, day)
        except ValueError:
            return None

    def _extract_year(self, text):
        search = self.YEAR_PATTERN.search(text)
        return int(search.group(1)) if search else None

    def _end_of_month(self, start_date):
        if start_date.month == 12:
            return date(start_date.year, 12, 31)
        return date(start_date.year, start_date.month + 1, 1) - timedelta(days=1)
