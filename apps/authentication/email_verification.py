"""
Email verification service for validating email addresses exist
Supports Google API and Abstract API for email verification
"""

import logging
import requests
from django.conf import settings
from rest_framework import serializers

logger = logging.getLogger("django")


class EmailVerificationService:
    """
    Service to verify if an email address actually exists
    Uses Abstract Email Reputation API (free tier)
    """

    @staticmethod
    def verify_email(email: str) -> dict:
        """
        Verify if an email address exists using Abstract API
        
        Args:
            email: Email address to verify
            
        Returns:
            dict with keys:
            - is_valid: bool - whether email is valid/exists
            - reason: str - reason if invalid
            - deliverable: bool - whether email can receive messages
            - is_smtp_valid: bool - SMTP verification result
        """
        
        # Get API key from settings
        abstract_api_key = getattr(settings, 'ABSTRACT_API_KEY', None)
        
        if not abstract_api_key:
            logger.warning("ABSTRACT_API_KEY not configured, skipping email verification")
            return {
                "is_valid": True,  # Default to allowing if no API key
                "reason": "Email verification disabled",
                "deliverable": True,
                "is_smtp_valid": True
            }
        
        try:
            # Use Abstract Email Reputation API (since your account only has Email Reputation)
            url = "https://emailreputation.abstractapi.com/v1/"
            
            params = {
                "api_key": abstract_api_key,
                "email": email.lower().strip(),
            }
            
            response = requests.get(url, params=params, timeout=10)
            
            try:
                data = response.json()
            except ValueError:
                data = {}
            
            if response.status_code != 200:
                message = data.get("error", {}).get("message") if isinstance(data.get("error"), dict) else data.get("error")
                reason = message or f"Abstract API returned HTTP {response.status_code}"
                logger.error(f"Email verification failed for {email}: {reason}")

                if response.status_code in (401, 403):
                    return {
                        "is_valid": False,
                        "reason": "Email verification API key invalid or unauthorized.",
                        "deliverable": False,
                        "is_smtp_valid": False,
                        "service_error": True,
                    }

                if 400 <= response.status_code < 500:
                    return {
                        "is_valid": False,
                        "reason": "Invalid email verification request.",
                        "deliverable": False,
                        "is_smtp_valid": False,
                        "service_error": True,
                    }

                # For server errors, keep fallback behavior to avoid blocking when the verification service is temporarily unavailable.
                return {
                    "is_valid": True,
                    "reason": "Verification service error",
                    "deliverable": True,
                    "is_smtp_valid": True,
                    "service_error": True,
                }
            
            email_deliverability = data.get("email_deliverability", {}) or {}
            status = (email_deliverability.get("status") or "").lower()
            is_format_valid = email_deliverability.get("is_format_valid", False)
            is_smtp_valid = email_deliverability.get("is_smtp_valid", False)
            
            is_valid = is_format_valid and status in ["deliverable", "risky"]
            
            result = {
                "is_valid": is_valid,
                "reason": email_deliverability.get("status_detail", "unknown"),
                "deliverable": status == "deliverable",
                "is_smtp_valid": is_smtp_valid,
                "service_error": False,
            }
            
            logger.info(f"Email verification for {email}: {result}")
            return result
            
        except requests.exceptions.Timeout:
            logger.error(f"Email verification timeout for {email}")
            return {
                "is_valid": True,  # Allow on timeout
                "reason": "Verification timeout",
                "deliverable": True,
                "is_smtp_valid": True,
                "service_error": True,
            }
        except requests.exceptions.RequestException as e:
            logger.error(f"Email verification request error for {email}: {str(e)}")
            return {
                "is_valid": True,  # Allow on network error
                "reason": "Verification service error",
                "deliverable": True,
                "is_smtp_valid": True,
                "service_error": True,
            }
        except Exception as e:
            logger.error(f"Unexpected error during email verification: {str(e)}")
            return {
                "is_valid": True,  # Allow on unexpected error
                "reason": "Unexpected verification error",
                "deliverable": True,
                "is_smtp_valid": True,
                "service_error": True,
            }

    @staticmethod
    def validate_email_for_signup(email: str) -> tuple[bool, str]:
        """
        Validate email for manager signup
        
        Args:
            email: Email address to validate
            
        Returns:
            tuple: (is_valid: bool, error_message: str or None)
        """
        
        email = email.lower().strip()
        
        # Basic format check
        if "@" not in email or "." not in email.split("@")[1]:
            return False, "Please provide a valid email address format."
        
        # Verify email exists
        verification_result = EmailVerificationService.verify_email(email)
        
        if verification_result.get("service_error") and not verification_result["is_valid"]:
            return False, (
                "Email verification failed because the verification service is not configured correctly. "
                "Please verify your Abstract API key and try again."
            )

        if not verification_result["is_valid"]:
            return False, (
                "The email address provided does not exist or is invalid. "
                "Please verify and try again with a valid email address."
            )
        
        if not verification_result.get("deliverable", True):
            return False, (
                "The email address cannot receive messages. "
                "Please use a valid, active email address."
            )
        
        return True, None
