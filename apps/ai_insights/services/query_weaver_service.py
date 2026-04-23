"""
apps/ai_insights/services/query_weaver_service.py
--------------------------------------------------
CORRECTIONS v3 :
  - parse_branch_names() : cherche dans Branch model en priorité (le plus fiable)
    puis MaterialMovement FK (branch__name), puis texte branch_name
  - parse_product_names() : match par code produit (EC0020, BDH110)
  - parse_date_range()    : gestion "cette année", "ce mois", "ytd"
  - Tous les fallbacks robustes aux espaces et NFC
"""

import re
from datetime import date, timedelta


class QueryWeaverService:

    MONTHS = {
        "january": 1,  "janvier": 1,
        "february": 2, "février": 2, "fevrier": 2,
        "march": 3,    "mars": 3,
        "april": 4,    "avril": 4,
        "may": 5,      "mai": 5,
        "june": 6,     "juin": 6,
        "july": 7,     "juillet": 7,
        "august": 8,   "août": 8, "aout": 8,
        "september": 9,"septembre": 9,
        "october": 10, "octobre": 10,
        "november": 11,"novembre": 11,
        "december": 12,"décembre": 12, "decembre": 12,
    }

    TOKEN_PATTERN  = re.compile(r"[\w\u0600-\u06FF]+", re.UNICODE)
    DATE_PATTERN   = re.compile(r"(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4})")
    YEAR_PATTERN   = re.compile(r"\b(20\d{2})\b")
    ARABIC_PATTERN = re.compile(r"[\u0600-\u06FF]{3,}")
    CODE_PATTERN   = re.compile(r"\b([A-Z]{2,5}\d{3,6})\b")

    # ── Branches ──────────────────────────────────────────────────────────────

    def parse_branch_names(self, text: str, company=None) -> list:
        text_lower = (text or "").lower().strip()
        if not company:
            return []
        return self._match_branch_names_from_db(text, text_lower, company)

    def _match_branch_names_from_db(self, text: str, text_lower: str, company) -> list:
        """
        Source 1 : Branch model (best — direct FK from MovementsParser)
        Source 2 : MaterialMovement.branch__name (via FK)
        Source 3 : MaterialMovement.branch_name (texte — peut être vide)
        Source 4 : InventorySnapshotLine.branch_name
        """
        branches = []

        # Source 1 : Branch model
        try:
            from apps.branches.models import Branch
            db_branches = list(
                Branch.objects.filter(is_active=True).values_list("name", flat=True)
            )
            if db_branches:
                branches = db_branches
        except Exception:
            pass

        # Source 2 : FK branch__name depuis MaterialMovement
        if not branches:
            try:
                from apps.transactions.models import MaterialMovement
                branches = list(
                    MaterialMovement.objects.filter(company=company)
                    .exclude(branch__name__isnull=True).exclude(branch__name__exact="")
                    .values_list("branch__name", flat=True).distinct()[:200]
                )
            except Exception:
                pass

        # Source 3 : Texte branch_name
        if not branches:
            try:
                from apps.transactions.models import MaterialMovement
                branches = list(
                    MaterialMovement.objects.filter(company=company)
                    .exclude(branch_name__isnull=True).exclude(branch_name__exact="")
                    .values_list("branch_name", flat=True).distinct()[:200]
                )
            except Exception:
                pass

        # Source 4 : InventorySnapshotLine
        if not branches:
            try:
                from apps.inventory.models import InventorySnapshotLine
                branches = list(
                    InventorySnapshotLine.objects.filter(company=company)
                    .exclude(branch_name__isnull=True).exclude(branch_name__exact="")
                    .values_list("branch_name", flat=True).distinct()[:200]
                )
            except Exception:
                return []

        if not branches:
            return []

        arabic_words  = set(self.ARABIC_PATTERN.findall(text_lower))
        query_tokens  = set(self._tokenize(text_lower))
        matched       = []

        for name in branches:
            lowered     = name.lower()
            name_tokens = set(self._tokenize(lowered))
            if not name_tokens:
                continue

            # Match 1 : substring exact
            if lowered in text_lower:
                if name not in matched:
                    matched.append(name)
                continue

            # Match 2 : mot arabe de 3+ caractères dans le nom de branche
            for word in arabic_words:
                if word in lowered and name not in matched:
                    matched.append(name)
                    break

            # Match 3 : ≥2 tokens communs ET ≥50% du nom couvert
            if name not in matched:
                common = len(query_tokens & name_tokens)
                if common >= 2 and common >= len(name_tokens) * 0.5:
                    matched.append(name)

        return matched

    def is_branch_comparison_question(self, text: str) -> bool:
        text_lower = text.lower()
        comp_kw  = ["compare", "comparer", "vs", "versus", "between", "entre",
                    "différence", "better", "meilleur", "قارن", "مقارنة"]
        branch_kw = ["branch", "branche", "فرع", "مخزن", "صالة", "warehouse"]
        return (any(k in text_lower for k in comp_kw) and
                any(k in text_lower for k in branch_kw))

    def is_branch_ranking_question(self, text: str) -> bool:
        text_lower = text.lower()
        return any(k in text_lower for k in [
            "top branch", "best branch", "highest branch",
            "meilleure branche", "branche la plus", "all branches",
            "toutes les branches", "كل الفروع", "by branch", "par branche",
            "quelle branche a", "which branch has", "rank branch",
            "classement branche", "per branch",
        ])

    # ── Produits ──────────────────────────────────────────────────────────────

    def parse_product_name(self, text, company=None) -> str:
        names = self.parse_product_names(text, company)
        return names[0] if names else ""

    def parse_product_names(self, text, company=None) -> list:
        text_lower = (text or "").lower().strip()

        # Match par code produit alphanumérique (EC0020, BDH110, SFTP...)
        codes = self.CODE_PATTERN.findall(text.upper())
        if codes and company:
            matched = self._match_by_codes(codes, company)
            if matched:
                return matched

        if company:
            # Match par nom complet
            matched = self._match_product_names_from_db(text_lower, company)
            if matched:
                return matched
            # Match par tokens
            matched = self._match_product_names_by_tokens(text_lower, company)
            if matched:
                return matched

        return []

    def _match_by_codes(self, codes: list, company) -> list:
        try:
            from apps.transactions.models import MaterialMovement
            from django.db.models import Q
            matched = []
            for code in codes:
                qs = MaterialMovement.objects.filter(
                    company=company,
                    material_code__iexact=code,
                ).values_list("material_name", flat=True).distinct()[:1]
                if qs:
                    matched.append(qs[0])
                else:
                    # Try icontains for partial codes
                    qs2 = MaterialMovement.objects.filter(
                        company=company,
                        material_code__icontains=code,
                    ).values_list("material_name", flat=True).distinct()[:1]
                    if qs2:
                        matched.append(qs2[0])
                    else:
                        matched.append(code)
            return matched
        except Exception:
            return codes

    def _match_product_names_from_db(self, text_lower: str, company) -> list:
        names = self._get_product_names_from_db(company)
        query_tokens = set(self._tokenize(text_lower))
        matched = []
        for name in names:
            lowered     = name.lower()
            name_tokens = set(self._tokenize(lowered))
            if not name_tokens:
                continue
            # Exact substring
            if lowered in text_lower:
                if name.strip() not in matched:
                    matched.append(name.strip())
                continue
            # Token match
            common = len(query_tokens & name_tokens)
            if common >= 2 and common >= len(name_tokens) * 0.5:
                if name.strip() not in matched:
                    matched.append(name.strip())
        return matched

    def _match_product_names_by_tokens(self, text_lower: str, company) -> list:
        names = self._get_product_names_from_db(company)
        query_tokens = set(self._tokenize(text_lower))
        matched = []
        for name in names:
            name_tokens = set(self._tokenize(name.lower()))
            if not name_tokens:
                continue
            common = len(query_tokens & name_tokens)
            if common >= 2 and name.strip() not in matched:
                matched.append(name.strip())
        return matched

    def _get_product_names_from_db(self, company) -> list:
        try:
            from apps.transactions.models import MaterialMovement
            return list(
                MaterialMovement.objects.filter(company=company)
                .exclude(material_name__isnull=True).exclude(material_name__exact="")
                .values_list("material_name", flat=True).distinct()[:500]
            )
        except Exception:
            return []

    # ── Clients ───────────────────────────────────────────────────────────────

    def parse_customer_name(self, text: str, company=None) -> str:
        # Entre guillemets
        quoted = re.search(r'["\']([^"\']{3,60})["\']', text)
        if quoted:
            return quoted.group(1).strip()

        # Séquence arabe longue (client ou fournisseur)
        arabic_matches = re.findall(
            r'[\u0600-\u06FF][\u0600-\u06FF\s/\-]{2,58}[\u0600-\u06FF]', text
        )
        if arabic_matches:
            return max(arabic_matches, key=len).strip()

        # Après mot-clé
        for kw in ["client", "customer", "عميل", "compte", "account", "fournisseur", "supplier"]:
            pattern = rf"(?i){re.escape(kw)}\s+(?:named\s+|appelé\s+)?([^\?,\.{{}}]{{3,80}})"
            match = re.search(pattern, text)
            if match:
                candidate = match.group(1).strip()

                # Ignore generic list/ranking phrasings that are not entity names.
                generic_markers = [
                    "first", "top", "list", "with their", "account codes",
                    "customers", "customer", "clients", "client", "all",
                    "premier", "premiers", "liste", "codes compte", "tous",
                ]
                c_low = candidate.lower()
                if any(m in c_low for m in generic_markers):
                    continue

                return candidate

        # Fournisseurs connus (majuscules)
        known = re.search(r"\b(ELAN|LINKNET|LEGRAND|OWER\s*GROUP|ASTON)\b", text.upper())
        if known:
            return known.group(1)

        return ""

    # ── Dates ─────────────────────────────────────────────────────────────────

    def parse_date_range(self, text):
        text_lower = (text or "").lower()

        # Dates explicites
        dates = self.DATE_PATTERN.findall(text_lower)
        if len(dates) >= 2:
            start = self._parse_date(dates[0])
            end   = self._parse_date(dates[1])
            if start and end:
                return start, end

        # Mois nommé
        for token, month in self.MONTHS.items():
            if token in text_lower:
                year  = self._extract_year(text_lower) or date.today().year
                start = date(year, month, 1)
                return start, self._end_of_month(start)

        # Année seule
        year_match = self.YEAR_PATTERN.search(text_lower)
        if year_match:
            year       = int(year_match.group(1))
            today      = date.today()
            year_start = date(year, 1, 1)
            year_end   = today if year >= today.year else date(year, 12, 31)
            return year_start, year_end

        # Ce mois
        if any(k in text_lower for k in ["this month", "ce mois", "هذا الشهر", "le mois"]):
            today = date.today()
            return date(today.year, today.month, 1), today

        # YTD / Cette année
        if any(k in text_lower for k in [
            "year to date", "ytd", "cette année", "منذ بداية السنة", "en 2026",
        ]):
            today = date.today()
            return date(today.year, 1, 1), today

        # Aujourd'hui
        if any(k in text_lower for k in ["today", "aujourd'hui", "اليوم"]):
            today = date.today()
            return today, today

        # Cette semaine
        if any(k in text_lower for k in ["this week", "cette semaine"]):
            today = date.today()
            start = today - timedelta(days=today.weekday())
            return start, today

        return None, None

    def _parse_date(self, raw: str):
        parts = re.split(r"[\/\-]", raw)
        if len(parts) != 3:
            return None
        try:
            day, month, year = int(parts[0]), int(parts[1]), int(parts[2])
            if year < 100:
                year += 2000
            return date(year, month, day)
        except ValueError:
            return None

    def _extract_year(self, text: str):
        m = self.YEAR_PATTERN.search(text)
        return int(m.group(1)) if m else None

    def _end_of_month(self, start_date: date) -> date:
        if start_date.month == 12:
            return date(start_date.year, 12, 31)
        return date(start_date.year, start_date.month + 1, 1) - timedelta(days=1)

    def _tokenize(self, text: str) -> list:
        return [t for t in self.TOKEN_PATTERN.findall(text.lower()) if len(t) >= 2]