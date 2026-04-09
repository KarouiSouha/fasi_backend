import logging
import re
import unicodedata
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Set

import openpyxl
from django.db import transaction

logger = logging.getLogger(__name__)


HEADER_FINGERPRINTS = {
    "branches": ["الفرع", "العنوان", "رقم الهاتف"],
    "customers": ["اسم العميل", "رمز الحساب"],
    "movements": ["رمز  المادة", "حركة.1", "كمية  الادخلات"],
    "inventory": ["رمز المادة", "إجمالي كمية", "إجمالي قيمة"],
    "aging": ["الحالي", "1-30 يوم", "31-60 يوم", "أكثر من 330 يوم"],
}

FILENAME_HINTS = {
    "branches": ["فروع", "branch"],
    "customers": ["العملاء", "customer"],
    "movements": ["حركة", "movement"],
    "inventory": ["جرد", "inventory"],
    "aging": ["اعمار", "aging", "ذمم"],
}


def _to_decimal(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value)).quantize(Decimal("0.0001"))
    except (InvalidOperation, TypeError):
        return default


def _to_str(value: Any) -> str:
    """
    Convert any cell value to a clean string.

    Applies:
      - NFC Unicode normalisation  (handles composed/decomposed Arabic)
      - str.strip()                (removes ALL Unicode whitespace, including
                                    U+0020 regular space and U+00A0 non-breaking)

    This is the single, authoritative string-cleaning helper used by every
    parser in this module.  All movement-type values must go through here so
    that cells like 'ف بيع ' (with a trailing space) are stored as 'ف بيع'.
    """
    if value is None:
        return ""
    s = unicodedata.normalize("NFC", str(value))
    return s.strip()


def _is_number(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, (int, float, Decimal)):
        return True
    txt = _to_str(value)
    if not txt:
        return False
    try:
        Decimal(txt)
        return True
    except (InvalidOperation, TypeError):
        return False


def _to_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(str(value).strip(), fmt).date()
        except ValueError:
            continue
    return None


def _extract_account_code(account_str: str) -> Optional[str]:
    if not account_str:
        return None
    parts = account_str.split("-", 1)
    code = parts[0].strip()
    return code if code.isdigit() else None


def detect_file_type(filename: str, header_row: tuple) -> Optional[str]:
    filename_lower = filename.lower()
    for file_type, hints in FILENAME_HINTS.items():
        if any(hint in filename_lower or hint in filename for hint in hints):
            return file_type

    if not header_row:
        return None

    header_joined = " ".join(_to_str(h) for h in header_row if h is not None)
    for file_type, keywords in HEADER_FINGERPRINTS.items():
        if all(kw in header_joined for kw in keywords):
            return file_type

    return None


class BranchesParser:
    def parse(self, rows: List, company, extra_context=None) -> Dict:
        from apps.branches.models import Branch

        data_rows = [r for r in rows[1:] if r and r[0] is not None]
        created = updated = 0
        errors = []

        for i, row in enumerate(data_rows, start=2):
            try:
                with transaction.atomic():
                    branch_name = _to_str(row[0])
                    if not branch_name:
                        continue
                    _, was_created = Branch.objects.update_or_create(
                        name=branch_name,
                        defaults={
                            "address": _to_str(row[1]) if len(row) > 1 else None or None,
                            "phone": _to_str(row[2]) if len(row) > 2 else None or None,
                            "is_active": True,
                        },
                    )
                    created += was_created
                    updated += not was_created
            except Exception as e:
                errors.append({"row": i, "error": str(e)})

        return {"total": len(data_rows), "created": created, "updated": updated, "errors": errors}


class CustomersParser:
    def parse(self, rows: List, company, extra_context=None) -> Dict:
        from apps.customers.models import Customer

        data_rows = [r for r in rows[1:] if r and r[0] is not None]
        created = updated = 0
        errors = []
        seen_codes: Set[str] = set()

        for i, row in enumerate(data_rows, start=2):
            try:
                with transaction.atomic():
                    customer_name = _to_str(row[0])
                    account_code = _to_str(row[1]) if len(row) > 1 else ""
                    address = _to_str(row[2]) if len(row) > 2 else ""
                    area_code = _to_str(row[3]) if len(row) > 3 else ""
                    phone = _to_str(row[4]) if len(row) > 4 else ""
                    email = _to_str(row[5]) if len(row) > 5 else ""

                    if not customer_name:
                        continue
                    if not account_code:
                        account_code = f"AUTO-{i}"

                    _, was_created = Customer.objects.update_or_create(
                        company=company,
                        account_code=account_code,
                        defaults={
                            "name": customer_name,
                            "address": address or None,
                            "area_code": area_code or None,
                            "phone": phone or None,
                            "email": email or None,
                            "is_active": True,
                        },
                    )
                    seen_codes.add(account_code)
                    created += was_created
                    updated += not was_created
            except Exception as e:
                errors.append({"row": i, "error": str(e)})

        return {
            "total": len(data_rows),
            "created": created,
            "updated": updated,
            "errors": errors,
        }


class MovementsParser:
    BATCH_SIZE = 500

    def parse(self, rows: List, company, extra_context=None) -> Dict:
        from apps.transactions.models import MaterialMovement
        from apps.products.models import Product
        from apps.branches.models import Branch
        from apps.customers.models import Customer

        data_rows = [r for r in rows[1:] if r and len(r) > 1 and r[1] is not None]

        file_dates: List[date] = []
        parsed: List[tuple] = []

        for i, row in enumerate(data_rows, start=2):
            d = _to_date(row[4]) if len(row) > 4 else None
            parsed.append((i, row, d))
            if d is not None:
                file_dates.append(d)

        if not file_dates:
            return {
                "total": 0,
                "created": 0,
                "updated": 0,
                "errors": [{"row": 0, "error": "No valid dates found in file."}],
            }

        date_from = min(file_dates)
        date_to = max(file_dates)

        product_cache: Dict[str, Optional[Product]] = {}
        branch_cache: Dict[str, Optional[Branch]] = {}
        customer_cache: Dict[str, Optional[Customer]] = {}

        created = 0
        errors = []

        with transaction.atomic():
            deleted_count, _ = MaterialMovement.objects.filter(
                company=company,
                movement_date__gte=date_from,
                movement_date__lte=date_to,
            ).delete()

        for i, row, movement_date in parsed:
            try:
                with transaction.atomic():
                    material_code = _to_str(row[1])
                    if not material_code or movement_date is None:
                        if movement_date is None and material_code:
                            errors.append({"row": i, "error": f"Invalid date: {row[4]}"})
                        continue

                    # ── FIX: explicit .strip() on movement_type ──────────────
                    # _to_str already calls .strip(), but we add an extra
                    # explicit call here as a belt-and-suspenders guard.
                    # Some Excel files (e.g. حركة_المادة_الشنت) store the cell
                    # value as 'ف بيع ' (trailing space).  Without this guard
                    # those rows are stored with the space and never match
                    # queries for 'ف بيع', causing branches to vanish from charts.
                    movement_type = _to_str(row[5]).strip() if len(row) > 5 else ""

                    if material_code not in product_cache:
                        product_cache[material_code] = Product.objects.filter(
                            company=company, product_code=material_code
                        ).first()

                    raw_branch_name = _to_str(row[13]) if len(row) > 13 else ""
                    branch = None
                    if raw_branch_name:
                        if raw_branch_name not in branch_cache:
                            branch_obj, _ = Branch.objects.get_or_create(
                                name=raw_branch_name,
                                defaults={"is_active": True},
                            )
                            branch_cache[raw_branch_name] = branch_obj
                        branch = branch_cache.get(raw_branch_name)

                    customer_name = _to_str(row[14]) if len(row) > 14 else ""
                    if customer_name and customer_name not in customer_cache:
                        customer_cache[customer_name] = Customer.objects.filter(
                            company=company,
                            name__icontains=customer_name[:50],
                        ).first()

                    MaterialMovement.objects.create(
                        company=company,
                        product=product_cache.get(material_code),
                        category=_to_str(row[0]) or None,
                        material_code=material_code,
                        lab_code=_to_str(row[2]) or None,
                        material_name=_to_str(row[3]),
                        movement_date=movement_date,
                        movement_type=movement_type,   # guaranteed stripped
                        qty_in=_to_decimal(row[6]) if len(row) > 6 else None,
                        price_in=_to_decimal(row[7]) if len(row) > 7 else None,
                        total_in=_to_decimal(row[8]) if len(row) > 8 else None,
                        qty_out=_to_decimal(row[9]) if len(row) > 9 else None,
                        price_out=_to_decimal(row[10]) if len(row) > 10 else None,
                        total_out=_to_decimal(row[11]) if len(row) > 11 else None,
                        balance_price=_to_decimal(row[12]) if len(row) > 12 else None,
                        branch=branch,
                        customer_name=customer_name or None,
                        customer=customer_cache.get(customer_name),
                    )
                    created += 1
            except Exception as e:
                errors.append({"row": i, "error": str(e)})

        return {
            "total": len(data_rows),
            "created": created,
            "updated": 0,
            "date_range": {"from": str(date_from), "to": str(date_to)},
            "deleted_existing": deleted_count,
            "errors": errors,
        }


class InventoryParser:
    """
    Melts a horizontal inventory Excel (جرد) into vertical InventorySnapshotLine rows.

    Fixed columns at the start (always positions 0–2):
        0: الفهرس        → product_category
        1: رمز المادة    → product_code
        2: اسم المادة    → product_name

    Fixed columns at the end (detected by header keywords):
        إجمالي كمية           → boundary marker for dynamic branch area
        السعر / كلفة الشركة  → unit_cost (applied to all branch lines of the row)

    Dynamic branch area: all columns between index 3 and total_qty_idx (exclusive).
    Each consecutive pair (col i, col i+1) is one branch:
        col i    → qty    (header = branch name, e.g. "فرع الكريمية")
        col i+1  → value  (header usually starts with "قيمة …")

    Result: current-stock rows only in InventorySnapshotLine.
    """

    _TOTAL_QTY_KW = [
        "إجماليكمية", "اجماليكمية",
        "الكميةالإجمالية", "الكميةالاجمالية",
        "totalqty", "totalquantity", "quantitytotal",
    ]
    _UNIT_COST_KW = [
        "كلفةالشركة", "سعرالتكلفة", "تكلفة",
        "costprice", "unitcost", "avgcost", "averagecost",
    ]

    def parse(self, rows: List, company, extra_context=None) -> Dict:
        from apps.inventory.models import InventorySnapshotLine

        extra_context = extra_context or {}
        user = extra_context.get("user")
        source_file = extra_context.get("filename", "")

        if not rows:
            return {"total": 0, "created": 0, "updated": 0, "errors": []}

        headers = [(_to_str(h) or "").strip() for h in rows[0]]
        data_rows = [r for r in rows[1:] if r and len(r) > 1 and _to_str(r[1])]

        if not data_rows:
            return {"total": 0, "created": 0, "updated": 0, "errors": []}

        # ── Header normaliser (strips spaces, parens, punctuation) ──────────
        def _norm(s: str) -> str:
            for ch in (" ", "_", "-", "/", "(", ")", "،", ",", "."):
                s = s.replace(ch, "")
            return s.lower()

        def _find(keywords: List[str]) -> Optional[int]:
            for idx, h in enumerate(headers):
                nh = _norm(h)
                if any(k in nh for k in keywords):
                    return idx
            return None

        # ── Locate boundary: إجمالي كمية marks where dynamic area ends ──────
        total_qty_idx = _find(self._TOTAL_QTY_KW)
        if total_qty_idx is None:
            return {
                "total": 0, "created": 0, "updated": 0,
                "errors": [{"row": 0, "error": "Column 'إجمالي كمية' not found — invalid inventory format."}],
            }

        # unit_cost: keyword first, then positional fallback (total_qty_idx + 1)
        unit_cost_idx = _find(self._UNIT_COST_KW) or _find(["السعر", "price"])
        if unit_cost_idx is None:
            unit_cost_idx = total_qty_idx + 1

        # ── Detect branch pairs from header ────────────────────────────
        dynamic_start = 3                   # after category, code, name
        dynamic_end = total_qty_idx         # exclusive upper bound

        if dynamic_end <= dynamic_start:
            return {
                "total": 0, "created": 0, "updated": 0,
                "errors": [{"row": 0, "error": "No branch columns found between product columns and 'إجمالي كمية'."}],
            }

        # Each pair: (qty_col_idx, value_col_idx, branch_name)
        # branch_name = raw header of the qty column (first of the pair)
        branch_pairs: List[tuple] = []
        i = dynamic_start
        while i + 1 < dynamic_end:
            branch_name = headers[i].strip()
            if branch_name:
                branch_pairs.append((i, i + 1, branch_name))
            i += 2
        # If dynamic area has an odd width, the last column is silently skipped.

        if not branch_pairs:
            return {
                "total": 0, "created": 0, "updated": 0,
                "errors": [{"row": 0, "error": "No branch column pairs detected in header."}],
            }

        detected_branches = [bp[2] for bp in branch_pairs]
        logger.info(
            "[InventoryParser] %d branch pairs detected: %s",
            len(branch_pairs), detected_branches,
        )

        # ── Extract fiscal year from filename ───────────────────────────
        year_match = re.search(r'(?<!\d)(20\d{2})(?!\d)', source_file)
        if not year_match:
            raise ValueError(
                "The file name does not contain a valid year. "
                "Please rename your file to include the year "
                "(e.g., Inventory_End_of_Year_2025.xls)."
            )
        inventory_year = int(year_match.group(1))

        # ── Upsert: replace current stock for the company ───────────────
        company_name = company.name if company else extra_context.get("company_name", "")
        if company:
            old_qs = InventorySnapshotLine.objects.filter(company=company)
            deleted_count = old_qs.count()
            old_qs.delete()
            if deleted_count:
                logger.info(
                    "[InventoryParser] Replaced %d existing inventory line(s) for "
                    "company=%s year=%d.",
                    deleted_count, company_name, inventory_year,
                )

        # ── Melt: 1 Excel row → len(branch_pairs) InventorySnapshotLine rows ──
        lines_created = 0
        errors = []

        for row_idx, row in enumerate(data_rows, start=2):
            try:
                product_category = _to_str(row[0]) if len(row) > 0 else ""
                product_code     = _to_str(row[1]) if len(row) > 1 else ""
                product_name     = _to_str(row[2]) if len(row) > 2 else ""

                if not product_code:
                    continue

                unit_cost = _to_decimal(row[unit_cost_idx]) if len(row) > unit_cost_idx else Decimal("0")

                lines: List[InventorySnapshotLine] = []
                for qty_idx, val_idx, branch_name in branch_pairs:
                    qty = _to_decimal(row[qty_idx]) if len(row) > qty_idx else Decimal("0")
                    val = _to_decimal(row[val_idx]) if len(row) > val_idx else Decimal("0")
                    lines.append(
                        InventorySnapshotLine(
                            company=company,
                            company_name=company_name,
                            inventory_year=inventory_year,
                            source_file=source_file,
                            uploaded_by=user,
                            product_category=product_category,
                            product_code=product_code,
                            product_name=product_name,
                            branch_name=branch_name,
                            quantity=qty,
                            unit_cost=unit_cost,
                            line_value=val,
                        )
                    )

                if lines:
                    InventorySnapshotLine.objects.bulk_create(lines, ignore_conflicts=True)
                    lines_created += len(lines)

            except Exception as e:
                errors.append({"row": row_idx, "error": str(e)})

        # Roll back empty import only if every row failed
        if lines_created == 0 and len(data_rows) > 0:
            return {
                "total": len(data_rows), "created": 0, "updated": 0,
                "errors": errors or [{"row": 0, "error": "No lines were imported."}],
            }

        return {
            "total": len(data_rows),
            "created": lines_created,
            "products_count": len(data_rows) - len(errors),
            "updated": 0,
            "snapshot_id": "00000000-0000-0000-0000-000000000001",
            "inventory_year": inventory_year,
            "branches_detected": detected_branches,
            "errors": errors,
        }


class AgingParser:
    def parse(self, rows: List, company, extra_context=None) -> Dict:
        from apps.aging.models import AgingReceivable, AgingSnapshot
        from apps.customers.models import Customer

        extra_context = extra_context or {}
        user = extra_context.get("user")
        source_file = extra_context.get("filename", "")

        # ── Validate: year must appear in the filename ────────────────────────
        year_match = re.search(r'(?<!\d)(20\d{2})(?!\d)', source_file)
        if not year_match:
            raise ValueError(
                "The file name does not contain a valid year. "
                "Please rename your file by including the year "
                "(e.g., Aging 2025.xlsx)."
            )
        aging_year = int(year_match.group(1))

        data_rows = [r for r in rows[1:] if r and len(r) > 1 and r[1] is not None]
        errors = []

        customer_map = {
            c.account_code: c
            for c in Customer.objects.filter(company=company)
        }

        # ── Upsert: delete existing snapshot for same company + year ─────────
        old_qs = AgingSnapshot.objects.filter(company=company, aging_year=aging_year)
        deleted_count = old_qs.count()
        old_qs.delete()
        if deleted_count:
            logger.info(
                "[AgingParser] Replaced %d existing snapshot(s) for "
                "company=%s year=%d.",
                deleted_count, company, aging_year,
            )

        # ── Create fresh snapshot for this import session ────────────────────
        snapshot = AgingSnapshot.objects.create(
            company=company,
            aging_year=aging_year,
            source_file=source_file,
            uploaded_by=user,
        )

        lines: List[AgingReceivable] = []

        for i, row in enumerate(data_rows, start=2):
            try:
                account = _to_str(row[1])
                if not account:
                    continue
                if any(kw in account for kw in ["الإجمالي", "المجموع", "Total"]):
                    continue

                account_code = _extract_account_code(account) or f"RAW-{account[:30]}"
                customer = customer_map.get(account_code)

                def sd(idx: int) -> Decimal:
                    val = row[idx] if len(row) > idx else None
                    if isinstance(val, str) and val.startswith("="):
                        return Decimal("0")
                    return _to_decimal(val)

                buckets = {
                    "current": sd(2),
                    "d1_30": sd(3),
                    "d31_60": sd(4),
                    "d61_90": sd(5),
                    "d91_120": sd(6),
                    "d121_150": sd(7),
                    "d151_180": sd(8),
                    "d181_210": sd(9),
                    "d211_240": sd(10),
                    "d241_270": sd(11),
                    "d271_300": sd(12),
                    "d301_330": sd(13),
                    "over_330": sd(14),
                }
                total = sum(buckets.values())

                lines.append(AgingReceivable(
                    snapshot=snapshot,
                    company=company,
                    customer=customer,
                    account=account,
                    account_code=account_code,
                    total=total,
                    **buckets,
                ))
            except Exception as e:
                errors.append({"row": i, "error": str(e)})

        with transaction.atomic():
            AgingReceivable.objects.bulk_create(lines)

        return {
            "total": len(data_rows),
            "created": len(lines),
            "updated": 0,
            "snapshot_id": str(snapshot.id),
            "aging_year": aging_year,
            "errors": errors,
        }


PARSERS = {
    "branches": BranchesParser,
    "customers": CustomersParser,
    "movements": MovementsParser,
    "inventory": InventoryParser,
    "aging": AgingParser,
}


def get_parser(file_type: str):
    cls = PARSERS.get(file_type)
    if not cls:
        raise ValueError(f"No parser registered for file type: '{file_type}'")
    return cls()


def parse_excel_file(
    file_obj,
    filename: str,
    company,
    file_type: str = None,
    extra_context: Dict = None,
) -> Dict:
    extra_context = extra_context or {}

    try:
        wb = openpyxl.load_workbook(file_obj, read_only=True, data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        wb.close()
    except Exception as e:
        raise ValueError(f"Cannot read Excel file '{filename}': {e}")

    if not rows:
        raise ValueError("The uploaded file is empty.")

    if not file_type:
        file_type = detect_file_type(filename, rows[0])
        if not file_type:
            raise ValueError(
                f"Cannot determine file type for '{filename}'. "
                "Rename the file to include one of: فروع, العملاء, حركة, جرد, اعمار — "
                "or pass file_type explicitly."
            )

    parser = get_parser(file_type)
    result = parser.parse(rows, company, extra_context)
    result["file_type"] = file_type
    return result