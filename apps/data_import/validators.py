"""
apps/data_import/validators.py

Comprehensive validation system for Excel imports.
Validates structure, data types, required fields, and business rules.
"""

import re
import unicodedata
from typing import List, Dict, Any, Optional, Tuple
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from .templates import (
    TemplateDefinition,
    ColumnDefinition,
    DataType,
    get_template,
)


class ValidationError(Exception):
    """Raised when validation fails."""

    def __init__(self, error_code: str, message: str, severity: str = "error"):
        """
        Initialize validation error.
        
        Args:
            error_code: Machine-readable error code
            message: Human-readable error message
            severity: "error", "warning", or "info"
        """
        self.error_code = error_code
        self.message = message
        self.severity = severity
        super().__init__(message)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API response."""
        return {
            "code": self.error_code,
            "message": self.message,
            "severity": self.severity,
        }


class ValidationResult:
    """Result of validation with detailed error information."""

    def __init__(self, file_type: str, is_valid: bool, template: Optional[TemplateDefinition] = None):
        self.file_type = file_type
        self.is_valid = is_valid
        self.template = template
        self.structure_errors: List[Dict[str, Any]] = []  # Header/structure problems
        self.row_errors: List[Dict[str, Any]] = []  # Data validation errors
        self.warnings: List[Dict[str, Any]] = []  # Non-blocking issues
        self.summary: Dict[str, Any] = {}

    def add_structure_error(
        self,
        error_code: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Add a structure validation error."""
        error = {
            "code": error_code,
            "message": message,
            "type": "structure",
        }
        if details:
            error.update(details)
        self.structure_errors.append(error)
        self.is_valid = False

    def add_row_error(
        self,
        row_index: int,
        column_name: str,
        error_code: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Add a data validation error for a specific cell."""
        error = {
            "row": row_index,
            "column": column_name,
            "code": error_code,
            "message": message,
            "type": "data",
        }
        if details:
            error.update(details)
        self.row_errors.append(error)

    def add_warning(
        self,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Add a non-blocking warning."""
        warning = {
            "message": message,
            "type": "warning",
        }
        if details:
            warning.update(details)
        self.warnings.append(warning)

    def set_summary(self, summary: Dict[str, Any]) -> None:
        """Set validation summary statistics."""
        self.summary = summary

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API response."""
        return {
            "file_type": self.file_type,
            "is_valid": self.is_valid,
            "structure_errors": self.structure_errors,
            "row_errors": self.row_errors[:100],  # Limit to 100 errors in response
            "row_errors_count": len(self.row_errors),
            "warnings": self.warnings,
            "summary": self.summary,
        }

    @property
    def has_critical_errors(self) -> bool:
        """Check if validation has structure errors (non-recoverable)."""
        return len(self.structure_errors) > 0

    @property
    def error_count(self) -> int:
        """Get total error count."""
        return len(self.structure_errors) + len(self.row_errors)


class ExcelHelper:
    """Helper functions for Excel data processing."""

    @staticmethod
    def normalize_string(value: Any) -> str:
        """
        Normalize string values: NFC normalization + strip.
        Handles Arabic correctly.
        """
        if value is None:
            return ""
        s = unicodedata.normalize("NFC", str(value))
        return s.strip()

    @staticmethod
    def is_empty_row(row: List[Any], min_values: int = 1) -> bool:
        """Check if a row has enough non-empty values."""
        non_empty = sum(1 for cell in row if cell is not None and str(cell).strip())
        return non_empty < min_values

    @staticmethod
    def to_decimal(value: Any) -> Optional[Decimal]:
        """Convert value to Decimal, return None if invalid."""
        if value is None or value == "":
            return None
        try:
            return Decimal(str(value)).quantize(Decimal("0.0001"))
        except (InvalidOperation, TypeError, ValueError):
            return None

    @staticmethod
    def to_date(value: Any) -> Optional[date]:
        """Convert value to date, return None if invalid."""
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        
        date_formats = ["%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y"]
        for fmt in date_formats:
            try:
                return datetime.strptime(str(value).strip(), fmt).date()
            except ValueError:
                continue
        return None

    @staticmethod
    def to_integer(value: Any) -> Optional[int]:
        """Convert value to integer, return None if invalid."""
        if value is None or value == "":
            return None
        try:
            return int(value)
        except (ValueError, TypeError):
            return None


class ColumnValidator:
    """Validates individual cell values against column definition."""

    def __init__(self, column: ColumnDefinition):
        self.column = column

    def validate(self, value: Any, row_index: int) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """
        Validate a cell value.
        
        Returns:
            (is_valid, error_dict)
        """
        # Handle empty values
        if value is None or (isinstance(value, str) and not value.strip()):
            if self.column.required:
                return False, {
                    "code": "MISSING_REQUIRED_FIELD",
                    "message": f"Required field '{self.column.arabic_name}' is empty",
                }
            return True, None

        normalized = ExcelHelper.normalize_string(value)

        # Validate by data type
        if self.column.data_type == DataType.STRING:
            if not normalized:
                if self.column.required:
                    return False, {
                        "code": "EMPTY_STRING",
                        "message": f"Field '{self.column.arabic_name}' cannot be empty",
                    }
            return True, None

        elif self.column.data_type == DataType.DECIMAL:
            decimal_val = ExcelHelper.to_decimal(value)
            if decimal_val is None:
                return False, {
                    "code": "INVALID_DECIMAL",
                    "message": f"Field '{self.column.arabic_name}' must be a valid number, got '{value}'",
                }

            if self.column.min_value is not None and decimal_val < Decimal(str(self.column.min_value)):
                return False, {
                    "code": "DECIMAL_TOO_SMALL",
                    "message": f"Field '{self.column.arabic_name}' must be >= {self.column.min_value}, got {decimal_val}",
                }

            if self.column.max_value is not None and decimal_val > Decimal(str(self.column.max_value)):
                return False, {
                    "code": "DECIMAL_TOO_LARGE",
                    "message": f"Field '{self.column.arabic_name}' must be <= {self.column.max_value}, got {decimal_val}",
                }
            return True, None

        elif self.column.data_type == DataType.INTEGER:
            int_val = ExcelHelper.to_integer(value)
            if int_val is None:
                return False, {
                    "code": "INVALID_INTEGER",
                    "message": f"Field '{self.column.arabic_name}' must be a whole number, got '{value}'",
                }

            if self.column.min_value is not None and int_val < int(self.column.min_value):
                return False, {
                    "code": "INTEGER_TOO_SMALL",
                    "message": f"Field '{self.column.arabic_name}' must be >= {int(self.column.min_value)}, got {int_val}",
                }

            if self.column.max_value is not None and int_val > int(self.column.max_value):
                return False, {
                    "code": "INTEGER_TOO_LARGE",
                    "message": f"Field '{self.column.arabic_name}' must be <= {int(self.column.max_value)}, got {int_val}",
                }
            return True, None

        elif self.column.data_type == DataType.DATE:
            date_val = ExcelHelper.to_date(value)
            if date_val is None:
                return False, {
                    "code": "INVALID_DATE",
                    "message": f"Field '{self.column.arabic_name}' must be a valid date (YYYY-MM-DD or DD/MM/YYYY), got '{value}'",
                }
            return True, None

        elif self.column.data_type == DataType.NUMBER:
            try:
                float(value)
                return True, None
            except (ValueError, TypeError):
                return False, {
                    "code": "INVALID_NUMBER",
                    "message": f"Field '{self.column.arabic_name}' must be a valid number, got '{value}'",
                }

        # Validate against allowed values
        if self.column.allowed_values:
            if normalized not in self.column.allowed_values:
                return False, {
                    "code": "INVALID_ENUM_VALUE",
                    "message": f"Field '{self.column.arabic_name}' must be one of: {', '.join(self.column.allowed_values)}, got '{value}'",
                }

        # Validate against pattern
        if self.column.pattern:
            if not re.match(self.column.pattern, normalized):
                return False, {
                    "code": "PATTERN_MISMATCH",
                    "message": f"Field '{self.column.arabic_name}' does not match required format, got '{value}'",
                }

        return True, None


class HeaderValidator:
    """Validates Excel file headers against template."""

    def __init__(self, template: TemplateDefinition):
        self.template = template

    def validate(self, header_row: List[Any]) -> Tuple[bool, List[Dict[str, Any]]]:
        """
        Validate header row structure.
        
        Returns:
            (is_valid, errors_list)
        """
        errors = []

        # Normalize headers
        headers = [ExcelHelper.normalize_string(h) for h in header_row]

        # Check column count
        expected_count = self.template.column_count
        actual_count = len(headers)

        if not self.template.allow_extra_columns and actual_count != expected_count:
            errors.append({
                "code": "COLUMN_COUNT_MISMATCH",
                "message": f"Expected {expected_count} columns, found {actual_count}",
                "expected": expected_count,
                "actual": actual_count,
            })

        if actual_count < expected_count:
            errors.append({
                "code": "MISSING_COLUMNS",
                "message": f"File has {actual_count} columns but template requires {expected_count}",
            })

        # Check headers match exactly
        if not self.template.allow_reordered_columns:
            for i, expected_col in enumerate(self.template.columns):
                if i >= len(headers):
                    errors.append({
                        "code": "MISSING_COLUMN",
                        "message": f"Column {i}: Expected '{expected_col.arabic_name}', but column doesn't exist",
                        "position": i,
                        "expected": expected_col.arabic_name,
                    })
                else:
                    actual = headers[i]
                    expected = expected_col.arabic_name
                    
                    if actual != expected:
                        errors.append({
                            "code": "WRONG_COLUMN_NAME",
                            "message": f"Column {i}: Expected '{expected}', found '{actual}'",
                            "position": i,
                            "expected": expected,
                            "actual": actual,
                        })

        # Check required columns present (if reordering allowed)
        if self.template.allow_reordered_columns:
            header_set = set(headers)
            for required_col in self.template.required_columns:
                if required_col.arabic_name not in header_set:
                    errors.append({
                        "code": "MISSING_REQUIRED_COLUMN",
                        "message": f"Required column '{required_col.arabic_name}' is missing",
                        "column": required_col.arabic_name,
                    })

        return len(errors) == 0, errors


class TemplateValidator:
    """Complete validation system for Excel files against templates."""

    def __init__(self, max_rows_to_validate: int = 1000):
        """
        Initialize validator.
        
        Args:
            max_rows_to_validate: Maximum number of data rows to validate
        """
        self.max_rows_to_validate = max_rows_to_validate

    def validate_file(
        self,
        rows: List[List[Any]],
        file_type: str,
    ) -> ValidationResult:
        """
        Comprehensive validation of entire file.
        
        Args:
            rows: List of rows from Excel file (header + data)
            file_type: Type of file to validate against
            
        Returns:
            ValidationResult with all errors and warnings
        """
        template = get_template(file_type)
        if not template:
            result = ValidationResult(file_type, False)
            result.add_structure_error(
                "UNKNOWN_FILE_TYPE",
                f"Unknown file type: {file_type}. Available types: {', '.join(['customers', 'branches', 'aging', 'inventory', 'movements'])}",
            )
            return result

        result = ValidationResult(file_type, True, template)

        # Check file is not empty
        if not rows:
            result.add_structure_error(
                "EMPTY_FILE",
                "File is empty. Please provide a file with headers and at least one data row.",
            )
            return result

        # Validate header
        header_validator = HeaderValidator(template)
        header_valid, header_errors = header_validator.validate(rows[0])

        if not header_valid:
            for error in header_errors:
                result.add_structure_error(
                    error["code"],
                    error["message"],
                    {k: v for k, v in error.items() if k not in ["code", "message"]},
                )

        # If header is invalid, return early
        if result.has_critical_errors:
            result.set_summary({
                "total_rows": len(rows) - 1,  # Excluding header
                "valid_rows": 0,
                "invalid_rows": len(rows) - 1,
            })
            return result

        # Validate data rows
        if len(rows) < 2:
            result.add_structure_error(
                "NO_DATA_ROWS",
                "File has header but no data rows. Please add at least one row of data.",
            )
            return result

        data_rows = rows[1:]
        valid_row_count = 0
        invalid_row_count = 0

        # Only validate subset of rows for large files
        rows_to_check = data_rows[:self.max_rows_to_validate]
        if len(data_rows) > self.max_rows_to_validate:
            result.add_warning(
                f"File has {len(data_rows)} rows. Validating first {self.max_rows_to_validate} rows.",
                {"rows_skipped": len(data_rows) - self.max_rows_to_validate},
            )

        for row_idx, row in enumerate(rows_to_check, start=2):  # Start from row 2 (after header)
            # Skip empty rows
            if ExcelHelper.is_empty_row(row):
                continue

            row_has_errors = False
            for col_idx, col_def in enumerate(template.columns):
                cell_value = row[col_idx] if col_idx < len(row) else None
                validator = ColumnValidator(col_def)
                is_valid, error_dict = validator.validate(cell_value, row_idx)

                if not is_valid:
                    result.add_row_error(
                        row_idx,
                        col_def.arabic_name,
                        error_dict["code"],
                        error_dict["message"],
                    )
                    row_has_errors = True

            if row_has_errors:
                invalid_row_count += 1
            else:
                valid_row_count += 1

        # Set summary
        result.set_summary({
            "total_rows": len(data_rows),
            "validated_rows": len(rows_to_check),
            "valid_rows": valid_row_count,
            "invalid_rows": invalid_row_count,
            "row_errors_count": len(result.row_errors),
        })

        # Update overall validity
        if result.row_errors:
            result.is_valid = False

        return result
