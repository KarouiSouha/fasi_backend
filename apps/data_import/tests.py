"""
Test cases for the Excel import validation system

Run with: python manage.py test apps.data_import.tests.ValidationTests
"""

from django.test import TestCase
from apps.data_import.validators import (
    TemplateValidator,
    ValidationResult,
    ExcelHelper,
    ColumnValidator,
    HeaderValidator,
)
from apps.data_import.templates import (
    CUSTOMERS_TEMPLATE,
    BRANCHES_TEMPLATE,
    MOVEMENTS_TEMPLATE,
    DataType,
)


class ExcelHelperTests(TestCase):
    """Test ExcelHelper utility functions"""

    def test_normalize_string(self):
        """Test string normalization"""
        # Test with Arabic characters
        result = ExcelHelper.normalize_string("  اسم العميل  ")
        self.assertEqual(result, "اسم العميل")

        # Test None handling
        result = ExcelHelper.normalize_string(None)
        self.assertEqual(result, "")

    def test_to_decimal(self):
        """Test decimal conversion"""
        result = ExcelHelper.to_decimal("123.456")
        self.assertIsNotNone(result)
        self.assertEqual(float(result), 123.456)

        result = ExcelHelper.to_decimal("invalid")
        self.assertIsNone(result)

    def test_to_date(self):
        """Test date conversion"""
        result = ExcelHelper.to_date("2024-04-14")
        self.assertIsNotNone(result)

        result = ExcelHelper.to_date("14/04/2024")
        self.assertIsNotNone(result)

        result = ExcelHelper.to_date("invalid")
        self.assertIsNone(result)

    def test_is_empty_row(self):
        """Test empty row detection"""
        self.assertTrue(ExcelHelper.is_empty_row([None, None, None]))
        self.assertTrue(ExcelHelper.is_empty_row(["", "", ""]))
        self.assertFalse(ExcelHelper.is_empty_row(["data", None, None]))


class ColumnValidatorTests(TestCase):
    """Test ColumnValidator for individual cells"""

    def test_string_validation_required(self):
        """Test string validation with required field"""
        from apps.data_import.templates import ColumnDefinition

        col = ColumnDefinition(
            name="Test",
            arabic_name="اختبار",
            data_type=DataType.STRING,
            required=True,
        )
        validator = ColumnValidator(col)

        # Valid string
        is_valid, error = validator.validate("test value", 2)
        self.assertTrue(is_valid)
        self.assertIsNone(error)

        # Empty string
        is_valid, error = validator.validate("", 2)
        self.assertFalse(is_valid)
        self.assertEqual(error["code"], "EMPTY_STRING")

    def test_decimal_validation(self):
        """Test decimal validation"""
        from apps.data_import.templates import ColumnDefinition

        col = ColumnDefinition(
            name="Amount",
            arabic_name="المبلغ",
            data_type=DataType.DECIMAL,
            min_value=0,
            max_value=1000,
        )
        validator = ColumnValidator(col)

        # Valid decimal
        is_valid, error = validator.validate("123.45", 2)
        self.assertTrue(is_valid)

        # Too small
        is_valid, error = validator.validate("-10", 2)
        self.assertFalse(is_valid)
        self.assertEqual(error["code"], "DECIMAL_TOO_SMALL")

        # Too large
        is_valid, error = validator.validate("2000", 2)
        self.assertFalse(is_valid)
        self.assertEqual(error["code"], "DECIMAL_TOO_LARGE")

    def test_date_validation(self):
        """Test date validation"""
        from apps.data_import.templates import ColumnDefinition

        col = ColumnDefinition(
            name="Date",
            arabic_name="التاريخ",
            data_type=DataType.DATE,
        )
        validator = ColumnValidator(col)

        # Valid date
        is_valid, error = validator.validate("2024-04-14", 2)
        self.assertTrue(is_valid)

        # Invalid date
        is_valid, error = validator.validate("invalid", 2)
        self.assertFalse(is_valid)
        self.assertEqual(error["code"], "INVALID_DATE")


class HeaderValidatorTests(TestCase):
    """Test HeaderValidator for Excel headers"""

    def test_exact_header_match(self):
        """Test exact header matching"""
        validator = HeaderValidator(CUSTOMERS_TEMPLATE)

        header = [
            "اسم العميل",
            "رمز الحساب",
            "العنوان التفصيلي",
            "رمز المنطقة",
            "رقم الهاتف1",
            "بريد الكتروني",
        ]
        is_valid, errors = validator.validate(header)
        self.assertTrue(is_valid)
        self.assertEqual(len(errors), 0)

    def test_wrong_column_name(self):
        """Test detection of wrong column name"""
        validator = HeaderValidator(CUSTOMERS_TEMPLATE)

        header = [
            "اسم العميل",
            "اسم الحساب",  # Wrong! Should be "رمز الحساب"
            "العنوان التفصيلي",
            "رمز المنطقة",
            "رقم الهاتف1",
            "بريد الكتروني",
        ]
        is_valid, errors = validator.validate(header)
        self.assertFalse(is_valid)
        self.assertTrue(
            any(e["code"] == "WRONG_COLUMN_NAME" for e in errors)
        )

    def test_wrong_column_count(self):
        """Test detection of wrong column count"""
        validator = HeaderValidator(CUSTOMERS_TEMPLATE)

        header = [
            "اسم العميل",
            "رمز الحساب",
            "العنوان التفصيلي",
            # Missing columns
        ]
        is_valid, errors = validator.validate(header)
        self.assertFalse(is_valid)
        self.assertTrue(
            any(e["code"] == "MISSING_COLUMNS" for e in errors)
        )

    def test_wrong_column_order(self):
        """Test detection of wrong column order"""
        validator = HeaderValidator(CUSTOMERS_TEMPLATE)

        header = [
            "رمز الحساب",  # Swapped
            "اسم العميل",  # Swapped
            "العنوان التفصيلي",
            "رمز المنطقة",
            "رقم الهاتف1",
            "بريد الكتروني",
        ]
        is_valid, errors = validator.validate(header)
        self.assertFalse(is_valid)
        self.assertTrue(
            any(e["code"] == "WRONG_COLUMN_NAME" for e in errors)
        )


class TemplateValidatorTests(TestCase):
    """Test complete TemplateValidator"""

    def test_empty_file(self):
        """Test validation of empty file"""
        validator = TemplateValidator()
        result = validator.validate_file([], "customers")

        self.assertFalse(result.is_valid)
        self.assertTrue(result.has_critical_errors)
        self.assertTrue(
            any(e["code"] == "EMPTY_FILE" for e in result.structure_errors)
        )

    def test_valid_file(self):
        """Test validation of valid file"""
        validator = TemplateValidator()

        rows = [
            [
                "اسم العميل",
                "رمز الحساب",
                "العنوان التفصيلي",
                "رمز المنطقة",
                "رقم الهاتف1",
                "بريد الكتروني",
            ],
            ["عميل 1", "ACC001", "العنوان 1", "R1", "123456", "email@test.com"],
            ["عميل 2", "ACC002", "العنوان 2", "R2", "234567", "email2@test.com"],
        ]
        result = validator.validate_file(rows, "customers")

        self.assertTrue(result.is_valid)
        self.assertFalse(result.has_critical_errors)
        self.assertEqual(result.summary["valid_rows"], 2)

    def test_invalid_data_types(self):
        """Test detection of invalid data types"""
        validator = TemplateValidator()

        rows = [
            [
                "#",
                "الحساب",
                "الحالي",
                "1-30 يوم",
                "31-60 يوم",
                # ... rest of aging headers
            ],
            ["1", "ACC001", "not_a_number", "100", "50"],  # Invalid decimal
        ]
        result = validator.validate_file(rows[:10], "aging")

        # Should have structure errors (not enough columns)
        # or data errors (invalid decimal)
        self.assertFalse(result.is_valid)

    def test_missing_required_fields(self):
        """Test detection of missing required fields"""
        validator = TemplateValidator()

        rows = [
            [
                "اسم العميل",
                "رمز الحساب",
                "العنوان التفصيلي",
                "رمز المنطقة",
                "رقم الهاتف1",
                "بريد الكتروني",
            ],
            ["", "ACC001", "العنوان 1", "R1", "123456", "email@test.com"],  # Empty name
            ["عميل 2", "", "العنوان 2", "R2", "234567", "email2@test.com"],  # Empty code
        ]
        result = validator.validate_file(rows, "customers")

        self.assertFalse(result.is_valid)
        self.assertTrue(len(result.row_errors) > 0)


class ValidationIntegrationTests(TestCase):
    """Integration tests with mock Excel files"""

    def test_customers_import_validation(self):
        """Test customer file validation"""
        validator = TemplateValidator()

        # Valid customer file
        rows = [
            [
                "اسم العميل",
                "رمز الحساب",
                "العنوان التفصيلي",
                "رمز المنطقة",
                "رقم الهاتف1",
                "بريد الكتروني",
            ],
            ["شركة أ", "ACC001", "طريق النيل", "CAI", "0201234567", "contact@company1.com"],
            ["شركة ب", "ACC002", "شارع الثورة", "GIZ", "0221234567", "contact@company2.com"],
        ]
        result = validator.validate_file(rows, "customers")

        self.assertTrue(result.is_valid)
        self.assertEqual(result.summary["total_rows"], 2)
        self.assertEqual(result.summary["valid_rows"], 2)

    def test_movements_import_validation(self):
        """Test movement file validation"""
        validator = TemplateValidator()

        rows = [
            [
                "الفهرس",
                "رمز  المادة",
                "رمز المعمل",
                "اسم   المادة",
                "تاريخ",
                "حركة.1",
                "كمية  الادخلات",
                "سعر  الادخلات",
                "اجمالي  الادخلات",
                "كمية  الاخراجات",
                "سعر  الاخراجات",
                "اجمالي   الاخراجات",
                "سعر  الرصيد",
                "الفرع",
                "العميل",
            ],
            [
                "1",
                "PROD001",
                "LAB001",
                "منتج 1",
                "2024-04-14",
                "ف بيع",
                "100",
                "50.50",
                "5050",
                "50",
                "50.50",
                "2525",
                "2525",
                "الفرع الرئيسي",
                "عميل 1",
            ],
        ]
        result = validator.validate_file(rows, "movements")

        # Should have no structure errors
        self.assertEqual(len(result.structure_errors), 0)

    def test_validation_result_serialization(self):
        """Test ValidationResult to_dict conversion"""
        validator = TemplateValidator()

        rows = [
            ["اسم العميل", "رمز الحساب"],  # Wrong template
            ["عميل 1", "ACC001"],
        ]
        result = validator.validate_file(rows, "customers")

        data = result.to_dict()
        self.assertIn("file_type", data)
        self.assertIn("is_valid", data)
        self.assertIn("structure_errors", data)
        self.assertIn("summary", data)


if __name__ == "__main__":
    import django
    from django.conf import settings
    from django.test.utils import get_runner

    if not settings.configured:
        settings.configure()

    django.setup()
    TestRunner = get_runner(settings)
    test_runner = TestRunner()
    failures = test_runner.run_tests(["__main__"])
