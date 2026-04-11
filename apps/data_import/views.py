"""
apps/data_import/views.py

Upload and import endpoints for Excel files.

Endpoints:
    POST   /api/import/upload/        — Upload and process a single Excel file
    GET    /api/import/logs/          — List import history for the company
    GET    /api/import/logs/{id}/     — Detail of a single import log
    DELETE /api/import/logs/{id}/     — Remove an import log record
    GET    /api/import/detect/        — Detect file type without importing (preview)
"""

import logging
from datetime import datetime, timezone

from rest_framework import status
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import ImportLog
from .serializers import ImportLogSerializer, ImportUploadSerializer
from .parsers.excel_parser import parse_excel_file, detect_file_type
import openpyxl

logger = logging.getLogger(__name__)


class ExcelUploadView(APIView):
    """
    POST /api/import/upload/

    Accepts a multipart form upload with an Excel file.
    Automatically detects the file type and triggers the appropriate parser.
    Creates an ImportLog entry and returns the result.

    Access rules:
        - Admin   : can import any file type
        - Manager : can import any file type for their company
        - Agent   : can import movements and inventory only
    """
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    AGENT_ALLOWED_TYPES = {"movements", "inventory"}

    def post(self, request):
        serializer = ImportUploadSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        file_obj = serializer.validated_data["file"]
        file_type_override = serializer.validated_data.get("file_type")
        company = request.user.company
        if not company:
            return Response(
                {"error": "Your account is not linked to a company. Contact your administrator."},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Create import log (pending)
        log = ImportLog.objects.create(
            company=company,
            imported_by=request.user,
            file_type=file_type_override or "movements",  # Placeholder until detected
            original_filename=file_obj.name,
            status=ImportLog.ImportStatus.PROCESSING,
            import_context={},
        )

        try:
            result = parse_excel_file(
                file_obj=file_obj,
                filename=file_obj.name,
                company=company,
                file_type=file_type_override,
                extra_context={"user": request.user, "filename": file_obj.name},
            )

            # Enforce agent file type restrictions
            detected_type = result["file_type"]
            if (
                request.user.is_agent
                and detected_type not in self.AGENT_ALLOWED_TYPES
            ):
                log.status = ImportLog.ImportStatus.FAILED
                log.error_details = [{"error": "Agents can only import movements and inventory files."}]
                log.completed_at = datetime.now(tz=timezone.utc)
                log.save()
                return Response(
                    {
                        "error": (
                            f"As an agent, you are not allowed to import "
                            f"'{detected_type}' files. Allowed types: "
                            f"{', '.join(self.AGENT_ALLOWED_TYPES)}."
                        )
                    },
                    status=status.HTTP_403_FORBIDDEN,
                )

            # ── Update ImportLog with results ──────────────────────────
            errors_list = result.get("errors", [])
            has_errors  = len(errors_list) > 0

            log.file_type     = detected_type
            log.row_count     = result["total"]
            if result["file_type"] == "inventory":
                log.success_count = result.get("products_count", result.get("total", 0))
            else:
                log.success_count = result.get("created", 0) + result.get("updated", 0)
            log.error_count   = len(errors_list)
            log.error_details = errors_list[:100]

            # Persist extra upsert metadata (date_range, deactivated, etc.)
            log.import_context.update({
                k: v for k, v in result.items()
                if k in ("date_range", "deleted_existing", "deleted_stale",
                         "deactivated")
            })

            log.status = (
                ImportLog.ImportStatus.PARTIAL  if has_errors and log.success_count > 0
                else ImportLog.ImportStatus.FAILED   if has_errors and log.success_count == 0
                else ImportLog.ImportStatus.SUCCESS
            )
            log.completed_at = datetime.now(tz=timezone.utc)
            log.save()

            if detected_type == "movements":
                try:
                    from .services.excel_vector_ingest import ExcelVectorIngestService

                    ExcelVectorIngestService().index_movements(
                        company,
                        date_range=result.get("date_range"),
                    )
                except Exception as exc:
                    logger.warning("[ExcelUploadView] Vector ingestion skipped: %s", exc)

            logger.info(
                f"[ExcelUploadView] Import complete: '{file_obj.name}' "
                f"({detected_type}) for company '{company.name}' "
                f"— created={result.get('created', 0)} updated={result.get('updated', 0)} "
                f"errors={log.error_count}."
            )

            return Response(
                {
                    "message":    self._build_message(log),
                    "import_log": ImportLogSerializer(log).data,
                    "result": {
                        "file_type":      detected_type,
                        "total_rows":     result["total"],
                        "created":        result.get("created", 0),
                        "updated":        result.get("updated", 0),
                        # Type-specific metadata
                        "deactivated":        result.get("deactivated"),        # customers
                        "deleted_stale":      result.get("deleted_stale"),      # aging
                        "deleted_existing":   result.get("deleted_existing"),   # movements
                        "date_range":         result.get("date_range"),         # movements
                        "snapshot_id":        result.get("snapshot_id"),        # inventory / aging
                        "inventory_year":     result.get("inventory_year"),     # inventory
                        "aging_year":         result.get("aging_year"),         # aging
                        "branches_detected":  result.get("branches_detected"),  # inventory
                        "errors":             errors_list[:20],
                    },
                },
                status=status.HTTP_201_CREATED,
            )

        except ValueError as e:
            log.status = ImportLog.ImportStatus.FAILED
            log.error_details = [{"error": str(e)}]
            log.completed_at = datetime.now(tz=timezone.utc)
            log.save()
            return Response(
                {"error": str(e), "import_log_id": str(log.id)},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        except Exception as e:
            logger.exception(f"[ExcelUploadView] Unexpected error: {e}")
            log.status = ImportLog.ImportStatus.FAILED
            log.error_details = [{"error": f"Internal error: {str(e)}"}]
            log.completed_at = datetime.now(tz=timezone.utc)
            log.save()
            return Response(
                {"error": "An unexpected error occurred during import. Please try again."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    @staticmethod
    def _build_message(log: ImportLog) -> str:
        if log.status == ImportLog.ImportStatus.SUCCESS:
            return (
                f"Import successful: {log.success_count} records imported "
                f"from '{log.original_filename}'."
            )
        if log.status == ImportLog.ImportStatus.PARTIAL:
            return (
                f"Partial import: {log.success_count} records imported, "
                f"{log.error_count} rows failed."
            )
        return f"Import failed: {log.error_count} errors encountered."


class DetectFileTypeView(APIView):
    """
    POST /api/import/detect/

    Accepts a file upload and returns the detected file type + preview
    of the first 5 rows WITHOUT persisting any data.
    Useful for UI validation before actual import.
    """
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        file_obj = request.FILES.get("file")
        if not file_obj:
            return Response(
                {"error": "No file uploaded."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        ext = file_obj.name.rsplit(".", 1)[-1].lower()
        if ext not in ("xlsx", "xls"):
            return Response(
                {"error": "Only .xlsx and .xls files are accepted."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            wb = openpyxl.load_workbook(file_obj, read_only=True, data_only=True)
            ws = wb.active
            all_rows = list(ws.iter_rows(values_only=True))
            wb.close()
        except Exception as e:
            return Response(
                {"error": f"Cannot read file: {e}"},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        if not all_rows:
            return Response(
                {"error": "The uploaded file is empty."},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        header_row = all_rows[0]
        detected_type = detect_file_type(file_obj.name, header_row)

        # Compte uniquement les lignes de données non vides (hors en-tête)
        data_rows = [
            r for r in all_rows[1:]
            if r and any(cell is not None and str(cell).strip() != "" for cell in r)
        ]
        actual_row_count = len(data_rows)

        headers = [str(h or "") for h in header_row]
        preview = []
        for row in all_rows[1:6]:
            preview.append(dict(zip(headers, [str(v or "") for v in row])))

        return Response({
            "filename": file_obj.name,
            "detected_file_type": detected_type,
            "headers": headers,
            "preview_rows": preview,
            "total_rows_estimate": actual_row_count,
        })

class ImportLogListView(APIView):
    """
    GET /api/import/logs/
    Returns import history for the authenticated user's company.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = ImportLog.objects.filter(
            company=request.user.company
        ).select_related("imported_by").order_by("-started_at")

        file_type = request.query_params.get("file_type")
        if file_type:
            qs = qs.filter(file_type=file_type)

        status_filter = request.query_params.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter)

        serializer = ImportLogSerializer(qs[:100], many=True)
        return Response({
            "count": qs.count(),
            "logs": serializer.data,
        })


class ImportLogDetailView(APIView):
    """
    GET    /api/import/logs/{id}/  → Import log details with full error list
    DELETE /api/import/logs/{id}/  → Delete an import log record
    """
    permission_classes = [IsAuthenticated]

    def _get_log(self, log_id, company):
        try:
            return ImportLog.objects.get(id=log_id, company=company)
        except ImportLog.DoesNotExist:
            return None

    def get(self, request, log_id):
        log = self._get_log(log_id, request.user.company)
        if not log:
            return Response({"error": "Import log not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(ImportLogSerializer(log).data)

    def delete(self, request, log_id):
        log = self._get_log(log_id, request.user.company)
        if not log:
            return Response({"error": "Import log not found."}, status=status.HTTP_404_NOT_FOUND)
        log.delete()
        return Response({"message": "Import log deleted."}, status=status.HTTP_200_OK)
