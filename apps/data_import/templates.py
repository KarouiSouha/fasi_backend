"""
apps/data_import/templates.py

Strict template definitions for each Excel file type.
Defines exact column names, order, data types, and validation rules.
"""

from typing import List, Dict, Any, Optional
from enum import Enum


class DataType(Enum):
    """Supported data types for column validation."""
    STRING = "string"
    NUMBER = "number"
    DECIMAL = "decimal"
    DATE = "date"
    INTEGER = "integer"


class ColumnDefinition:
    """Defines a single column in a template."""

    def __init__(
        self,
        name: str,
        arabic_name: str,
        data_type: DataType,
        required: bool = False,
        position: Optional[int] = None,
        min_value: Optional[float] = None,
        max_value: Optional[float] = None,
        pattern: Optional[str] = None,
        allowed_values: Optional[List[str]] = None,
        default_value: Any = None,
    ):
        """
        Initialize a column definition.
        
        Args:
            name: English column name
            arabic_name: Arabic column name (exact, as in template)
            data_type: DataType enum value
            required: Whether this column is mandatory
            position: Expected column position (0-based)
            min_value: Minimum value for numeric types
            max_value: Maximum value for numeric types
            pattern: Regex pattern for string validation
            allowed_values: List of allowed values for enumeration
            default_value: Default value if not provided
        """
        self.name = name
        self.arabic_name = arabic_name
        self.data_type = data_type
        self.required = required
        self.position = position
        self.min_value = min_value
        self.max_value = max_value
        self.pattern = pattern
        self.allowed_values = allowed_values
        self.default_value = default_value

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses."""
        return {
            "name": self.name,
            "arabic_name": self.arabic_name,
            "data_type": self.data_type.value,
            "required": self.required,
            "position": self.position,
        }


class TemplateDefinition:
    """Defines the complete structure for an Excel import template."""

    def __init__(
        self,
        file_type: str,
        display_name: str,
        arabic_name: str,
        description: str,
        columns: List[ColumnDefinition],
        allow_extra_columns: bool = False,
        allow_reordered_columns: bool = False,
    ):
        """
        Initialize a template definition.
        
        Args:
            file_type: Internal file type identifier
            display_name: English display name
            arabic_name: Arabic display name
            description: Description of the file purpose
            columns: List of ColumnDefinition objects
            allow_extra_columns: Allow additional columns beyond template
            allow_reordered_columns: Allow columns in different order
        """
        self.file_type = file_type
        self.display_name = display_name
        self.arabic_name = arabic_name
        self.description = description
        self.columns = columns
        self.allow_extra_columns = allow_extra_columns
        self.allow_reordered_columns = allow_reordered_columns

    @property
    def required_columns(self) -> List[ColumnDefinition]:
        """Get all required columns."""
        return [col for col in self.columns if col.required]

    @property
    def optional_columns(self) -> List[ColumnDefinition]:
        """Get all optional columns."""
        return [col for col in self.columns if not col.required]

    @property
    def column_names(self) -> List[str]:
        """Get exact Arabic column names in order."""
        return [col.arabic_name for col in self.columns]

    @property
    def column_count(self) -> int:
        """Get expected number of columns."""
        return len(self.columns)

    def get_column_by_name(self, arabic_name: str) -> Optional[ColumnDefinition]:
        """Find a column by its Arabic name."""
        for col in self.columns:
            if col.arabic_name == arabic_name:
                return col
        return None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses."""
        return {
            "file_type": self.file_type,
            "display_name": self.display_name,
            "arabic_name": self.arabic_name,
            "description": self.description,
            "column_count": self.column_count,
            "columns": [col.to_dict() for col in self.columns],
            "required_count": len(self.required_columns),
        }


# ─────────────────────────────────────────────────────────────────────────
# TEMPLATE DEFINITIONS (STRICT)
# ─────────────────────────────────────────────────────────────────────────

CUSTOMERS_TEMPLATE = TemplateDefinition(
    file_type="customers",
    display_name="Customers",
    arabic_name="العملاء",
    description="Import customer data with account codes and contact information",
    columns=[
        ColumnDefinition(
            name="Customer Name",
            arabic_name="اسم العميل",
            data_type=DataType.STRING,
            required=True,
            position=0,
        ),
        ColumnDefinition(
            name="Account Code",
            arabic_name="رمز الحساب",
            data_type=DataType.STRING,
            required=True,
            position=1,
        ),
        ColumnDefinition(
            name="Detailed Address",
            arabic_name="العنوان التفصيلي",
            data_type=DataType.STRING,
            required=False,
            position=2,
        ),
        ColumnDefinition(
            name="Region Code",
            arabic_name="رمز المنطقة",
            data_type=DataType.STRING,
            required=False,
            position=3,
        ),
        ColumnDefinition(
            name="Phone Number",
            arabic_name="رقم الهاتف1",
            data_type=DataType.STRING,
            required=False,
            position=4,
        ),
        ColumnDefinition(
            name="Email",
            arabic_name="بريد الكتروني",
            data_type=DataType.STRING,
            required=False,
            position=5,
        ),
    ],
    allow_extra_columns=False,
    allow_reordered_columns=False,
)

BRANCHES_TEMPLATE = TemplateDefinition(
    file_type="branches",
    display_name="Branches",
    arabic_name="الفروع",
    description="Import branch locations and contact information",
    columns=[
        ColumnDefinition(
            name="Branch",
            arabic_name="الفرع",
            data_type=DataType.STRING,
            required=True,
            position=0,
        ),
        ColumnDefinition(
            name="Address / Location",
            arabic_name="العنوان / الموقع",
            data_type=DataType.STRING,
            required=False,
            position=1,
        ),
        ColumnDefinition(
            name="Phone Number",
            arabic_name="رقم الهاتف",
            data_type=DataType.STRING,
            required=False,
            position=2,
        ),
    ],
    allow_extra_columns=False,
    allow_reordered_columns=False,
)

AGING_TEMPLATE = TemplateDefinition(
    file_type="aging",
    display_name="Aging of Receivables",
    arabic_name="أعمار الذمم",
    description="Import aging receivables report with time-based distribution",
    columns=[
        ColumnDefinition(
            name="#",
            arabic_name="#",
            data_type=DataType.INTEGER,
            required=False,
            position=0,
        ),
        ColumnDefinition(
            name="Account",
            arabic_name="الحساب",
            data_type=DataType.STRING,
            required=True,
            position=1,
        ),
        ColumnDefinition(
            name="Current",
            arabic_name="الحالي",
            data_type=DataType.DECIMAL,
            required=False,
            position=2,
            min_value=0,
        ),
        ColumnDefinition(
            name="1-30 Days",
            arabic_name="1-30 يوم",
            data_type=DataType.DECIMAL,
            required=False,
            position=3,
            min_value=0,
        ),
        ColumnDefinition(
            name="31-60 Days",
            arabic_name="31-60 يوم",
            data_type=DataType.DECIMAL,
            required=False,
            position=4,
            min_value=0,
        ),
        ColumnDefinition(
            name="61-90 Days",
            arabic_name="61-90 يوم",
            data_type=DataType.DECIMAL,
            required=False,
            position=5,
            min_value=0,
        ),
        ColumnDefinition(
            name="91-120 Days",
            arabic_name="91-120 يوم",
            data_type=DataType.DECIMAL,
            required=False,
            position=6,
            min_value=0,
        ),
        ColumnDefinition(
            name="121-150 Days",
            arabic_name="121-150 يوم",
            data_type=DataType.DECIMAL,
            required=False,
            position=7,
            min_value=0,
        ),
        ColumnDefinition(
            name="151-180 Days",
            arabic_name="151-180 يوم",
            data_type=DataType.DECIMAL,
            required=False,
            position=8,
            min_value=0,
        ),
        ColumnDefinition(
            name="181-210 Days",
            arabic_name="181-210 يوم",
            data_type=DataType.DECIMAL,
            required=False,
            position=9,
            min_value=0,
        ),
        ColumnDefinition(
            name="211-240 Days",
            arabic_name="211-240 يوم",
            data_type=DataType.DECIMAL,
            required=False,
            position=10,
            min_value=0,
        ),
        ColumnDefinition(
            name="241-270 Days",
            arabic_name="241-270 يوم",
            data_type=DataType.DECIMAL,
            required=False,
            position=11,
            min_value=0,
        ),
        ColumnDefinition(
            name="271-300 Days",
            arabic_name="271-300 يوم",
            data_type=DataType.DECIMAL,
            required=False,
            position=12,
            min_value=0,
        ),
        ColumnDefinition(
            name="301-330 Days",
            arabic_name="301-330 يوم",
            data_type=DataType.DECIMAL,
            required=False,
            position=13,
            min_value=0,
        ),
        ColumnDefinition(
            name="Over 330 Days",
            arabic_name="أكثر من 330 يوم",
            data_type=DataType.DECIMAL,
            required=False,
            position=14,
            min_value=0,
        ),
        ColumnDefinition(
            name="Total",
            arabic_name="المجموع",
            data_type=DataType.DECIMAL,
            required=False,
            position=15,
            min_value=0,
        ),
    ],
    allow_extra_columns=False,
    allow_reordered_columns=False,
)

INVENTORY_TEMPLATE = TemplateDefinition(
    file_type="inventory",
    display_name="Year-End Inventory",
    arabic_name="الجرد الأفقي",
    description="Import year-end inventory with quantities and values per branch",
    columns=[
        ColumnDefinition(
            name="Index",
            arabic_name="الفهرس",
            data_type=DataType.INTEGER,
            required=False,
            position=0,
        ),
        ColumnDefinition(
            name="Item Code",
            arabic_name="رمز المادة",
            data_type=DataType.STRING,
            required=True,
            position=1,
        ),
        ColumnDefinition(
            name="Item Name",
            arabic_name="اسم المادة",
            data_type=DataType.STRING,
            required=True,
            position=2,
        ),
        # Note: Branch columns are dynamic and handled separately in InventoryParser
        # Fixed columns at the end must include:
        # - إجمالي كمية (Total Quantity) - required
        # - السعر / كلفة الشركة (Unit Cost) - optional
    ],
    allow_extra_columns=True,
    allow_reordered_columns=False,
)

MOVEMENTS_TEMPLATE = TemplateDefinition(
    file_type="movements",
    display_name="Stock Movements",
    arabic_name="حركة المادة",
    description="Import item movements including inputs, outputs and balances",
    columns=[
        ColumnDefinition(
            name="Index",
            arabic_name="الفهرس",
            data_type=DataType.INTEGER,
            required=False,
            position=0,
        ),
        ColumnDefinition(
            name="Item Code",
            arabic_name="رمز  المادة",
            data_type=DataType.STRING,
            required=True,
            position=1,
        ),
        ColumnDefinition(
            name="Lab Code",
            arabic_name="رمز المعمل",
            data_type=DataType.STRING,
            required=False,
            position=2,
        ),
        ColumnDefinition(
            name="Item Name",
            arabic_name="اسم   المادة",
            data_type=DataType.STRING,
            required=True,
            position=3,
        ),
        ColumnDefinition(
            name="Date",
            arabic_name="تاريخ",
            data_type=DataType.DATE,
            required=True,
            position=4,
        ),
        ColumnDefinition(
            name="Movement Type",
            arabic_name="حركة.1",
            data_type=DataType.STRING,
            required=False,
            position=5,
        ),
        ColumnDefinition(
            name="Input Qty",
            arabic_name="كمية  الادخلات",
            data_type=DataType.DECIMAL,
            required=False,
            position=6,
            min_value=0,
        ),
        ColumnDefinition(
            name="Input Price",
            arabic_name="سعر  الادخلات",
            data_type=DataType.DECIMAL,
            required=False,
            position=7,
            min_value=0,
        ),
        ColumnDefinition(
            name="Total Input",
            arabic_name="اجمالي  الادخلات",
            data_type=DataType.DECIMAL,
            required=False,
            position=8,
            min_value=0,
        ),
        ColumnDefinition(
            name="Output Qty",
            arabic_name="كمية  الاخراجات",
            data_type=DataType.DECIMAL,
            required=False,
            position=9,
            min_value=0,
        ),
        ColumnDefinition(
            name="Output Price",
            arabic_name="سعر  الاخراجات",
            data_type=DataType.DECIMAL,
            required=False,
            position=10,
            min_value=0,
        ),
        ColumnDefinition(
            name="Total Output",
            arabic_name="اجمالي   الاخراجات",
            data_type=DataType.DECIMAL,
            required=False,
            position=11,
            min_value=0,
        ),
        ColumnDefinition(
            name="Balance Price",
            arabic_name="سعر  الرصيد",
            data_type=DataType.DECIMAL,
            required=False,
            position=12,
            min_value=0,
        ),
        ColumnDefinition(
            name="Branch",
            arabic_name="الفرع",
            data_type=DataType.STRING,
            required=False,
            position=13,
        ),
        ColumnDefinition(
            name="Customer",
            arabic_name="العميل",
            data_type=DataType.STRING,
            required=False,
            position=14,
        ),
    ],
    allow_extra_columns=False,
    allow_reordered_columns=False,
)


# Template registry
TEMPLATES: Dict[str, TemplateDefinition] = {
    "customers": CUSTOMERS_TEMPLATE,
    "branches": BRANCHES_TEMPLATE,
    "aging": AGING_TEMPLATE,
    "inventory": INVENTORY_TEMPLATE,
    "movements": MOVEMENTS_TEMPLATE,
}


def get_template(file_type: str) -> Optional[TemplateDefinition]:
    """Get a template by file type."""
    return TEMPLATES.get(file_type)


def list_templates() -> Dict[str, TemplateDefinition]:
    """Get all available templates."""
    return TEMPLATES.copy()
