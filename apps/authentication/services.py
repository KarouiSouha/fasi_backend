import logging
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import EmailMultiAlternatives
from django.core.mail import get_connection

logger = logging.getLogger("django")
User = get_user_model()


class EmailService:

    @staticmethod
    def send_admin_new_manager_request(manager: User) -> None:
        """
        Sends an HTML email to admins with a redirect button to the app.
        """
        admins = User.objects.filter(role=User.Role.ADMIN, is_active=True)
        admin_emails = list(admins.values_list("email", flat=True))

        if not admin_emails:
            logger.warning(f"No active admin found to notify about new manager [{manager.email}].")
            return

        company_name = manager.company_name or "Not provided"
        frontend_url = getattr(settings, "FRONTEND_URL", "http://localhost:5173")
        admin_url = f"{frontend_url}/dashboard/admin-verification"

        subject = f"[WEEG] New Manager Access Request — {manager.full_name}"

        # Plain text version (fallback)
        text_content = (
            f"Hello,\n\n"
            f"A new manager has just registered and is awaiting your approval.\n\n"
            f"Name     : {manager.full_name}\n"
            f"Email    : {manager.email}\n"
            f"Phone    : {manager.phone_number or 'Not provided'}\n"
            f"Company  : {company_name}\n\n"
            f"To approve or reject this request, click here:\n{admin_url}\n\n"
            f"Best regards,\nThe WEEG system"
        )

        # HTML version with button
        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head><meta charset="UTF-8"></head>
        <body style="font-family: Arial, sans-serif; background:#f4f4f4; margin:0; padding:20px;">
          <div style="max-width:600px; margin:0 auto; background:#fff; border-radius:8px; overflow:hidden; box-shadow:0 2px 8px rgba(0,0,0,0.1);">
            
            <!-- Header -->
            <div style="background:linear-gradient(135deg,#4f46e5,#7c3aed); padding:30px; text-align:center;">
              <h1 style="color:#fff; margin:0; font-size:28px; font-weight:bold;">WEEG</h1>
              <p style="color:#c7d2fe; margin:8px 0 0;">Financial Analytics & System Intelligence</p>
            </div>

            <!-- Body -->
            <div style="padding:30px;">
              <h2 style="color:#1e293b; margin-top:0;"> New Manager Access Request</h2>
              <p style="color:#475569;">A new manager has created an account and is waiting for your approval.</p>

              <!-- Info Card -->
              <div style="background:#f8fafc; border:1px solid #e2e8f0; border-radius:8px; padding:20px; margin:20px 0;">
                <table style="width:100%; border-collapse:collapse;">
                  <tr>
                    <td style="padding:8px 0; color:#64748b; width:120px;"> Name</td>
                    <td style="padding:8px 0; color:#1e293b; font-weight:bold;">{manager.full_name}</td>
                  </tr>
                  <tr>
                    <td style="padding:8px 0; color:#64748b;"> Email</td>
                    <td style="padding:8px 0; color:#1e293b;">{manager.email}</td>
                  </tr>
                  <tr>
                    <td style="padding:8px 0; color:#64748b;"> Phone</td>
                    <td style="padding:8px 0; color:#1e293b;">{manager.phone_number or 'Not provided'}</td>
                  </tr>
                  <tr>
                    <td style="padding:8px 0; color:#64748b;"> Company</td>
                    <td style="padding:8px 0; color:#1e293b;">{company_name}</td>
                  </tr>
                </table>
              </div>

              <!-- CTA Button -->
              <div style="text-align:center; margin:30px 0;">
                <a href="{admin_url}"
                   style="display:inline-block; background:linear-gradient(135deg,#4f46e5,#7c3aed); color:#fff;
                          text-decoration:none; padding:14px 32px; border-radius:8px; font-weight:bold;
                          font-size:16px; letter-spacing:0.5px;">
                   Review Request in WEEG
                </a>
              </div>

              <p style="color:#94a3b8; font-size:13px; text-align:center;">
                Or copy this link: <a href="{admin_url}" style="color:#4f46e5;">{admin_url}</a>
              </p>
            </div>

            <!-- Footer -->
            <div style="background:#f8fafc; padding:20px; text-align:center; border-top:1px solid #e2e8f0;">
              <p style="color:#94a3b8; font-size:12px; margin:0;">
                This email was automatically sent by the WEEG system.<br>
                Please do not reply to this email.
              </p>
            </div>
          </div>
        </body>
        </html>
        """

        connection = get_connection(timeout=5)
        msg = EmailMultiAlternatives(
            subject=subject,
            body=text_content,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=admin_emails,
            connection=connection,
        )
        msg.attach_alternative(html_content, "text/html")
        msg.send(fail_silently=False)

        logger.info(f"Email sent to {len(admin_emails)} admin(s) regarding [{manager.email}].")

    @staticmethod
    def send_manager_approved(manager: User) -> None:
        subject = "[WEEG] Your account has been approved — You can now log in"
        frontend_url = getattr(settings, "FRONTEND_URL", "http://localhost:5173")
        login_url = f"{frontend_url}/login"

        text_content = (
            f"Hello {manager.first_name},\n\n"
            f"Your WEEG Manager account has been approved.\n"
            f"Log in here: {login_url}\n\n"
            f"Best regards,\nThe WEEG team"
        )

        html_content = f"""
        <!DOCTYPE html>
        <html>
        <body style="font-family:Arial,sans-serif;background:#f4f4f4;margin:0;padding:20px;">
          <div style="max-width:600px;margin:0 auto;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.1);">
            <div style="background:linear-gradient(135deg,#4f46e5,#7c3aed);padding:30px;text-align:center;">
              <h1 style="color:#fff;margin:0;">WEEG</h1>
            </div>
            <div style="padding:30px;">
              <h2 style="color:#16a34a;"> Account Approved!</h2>
              <p style="color:#475569;">Hello <strong>{manager.first_name}</strong>,</p>
              <p style="color:#475569;">Great news! Your Manager account on the WEEG platform has been <strong>approved</strong>.</p>
              <p style="color:#475569;">You can now log in and start using the platform.</p>
              <div style="text-align:center;margin:30px 0;">
                <a href="{login_url}"
                   style="display:inline-block;background:linear-gradient(135deg,#4f46e5,#7c3aed);color:#fff;
                          text-decoration:none;padding:14px 32px;border-radius:8px;font-weight:bold;font-size:16px;">
                  Log in to WEEG
                </a>
              </div>
            </div>
            <div style="background:#f8fafc;padding:20px;text-align:center;border-top:1px solid #e2e8f0;">
              <p style="color:#94a3b8;font-size:12px;margin:0;">Automatic WEEG email — Do not reply.</p>
            </div>
          </div>
        </body>
        </html>
        """

        msg = EmailMultiAlternatives(subject, text_content, settings.DEFAULT_FROM_EMAIL, [manager.email])
        msg.attach_alternative(html_content, "text/html")
        msg.send(fail_silently=False)

    @staticmethod
    def send_manager_rejected(manager: User, reason: str) -> None:
        subject = "[WEEG] Your access request was not approved"

        text_content = (
            f"Hello {manager.first_name},\n\n"
            f"Your WEEG access request has not been approved.\n"
            f"Reason: {reason}\n\n"
            f"Best regards,\nThe WEEG team"
        )

        html_content = f"""
        <!DOCTYPE html>
        <html>
        <body style="font-family:Arial,sans-serif;background:#f4f4f4;margin:0;padding:20px;">
          <div style="max-width:600px;margin:0 auto;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.1);">
            <div style="background:linear-gradient(135deg,#4f46e5,#7c3aed);padding:30px;text-align:center;">
              <h1 style="color:#fff;margin:0;">WEEG</h1>
            </div>
            <div style="padding:30px;">
              <h2 style="color:#dc2626;"> Request Not Approved</h2>
              <p style="color:#475569;">Hello <strong>{manager.first_name}</strong>,</p>
              <p style="color:#475569;">We have reviewed your access request to the WEEG platform.</p>
              <div style="background:#fef2f2;border:1px solid #fecaca;border-radius:8px;padding:16px;margin:20px 0;">
                <p style="color:#dc2626;margin:0;"><strong>Reason for rejection:</strong></p>
                <p style="color:#7f1d1d;margin:8px 0 0;">{reason}</p>
              </div>
              <p style="color:#475569;">For any questions, please contact your administrator.</p>
            </div>
            <div style="background:#f8fafc;padding:20px;text-align:center;border-top:1px solid #e2e8f0;">
              <p style="color:#94a3b8;font-size:12px;margin:0;">Automatic WEEG email — Do not reply.</p>
            </div>
          </div>
        </body>
        </html>
        """

        msg = EmailMultiAlternatives(subject, text_content, settings.DEFAULT_FROM_EMAIL, [manager.email])
        msg.attach_alternative(html_content, "text/html")
        msg.send(fail_silently=False)

    @staticmethod
    def send_agent_credentials(agent: User, temporary_password: str, created_by: User) -> None:
        company_name = agent.company_name or "WEEG"
        frontend_url = getattr(settings, "FRONTEND_URL", "http://localhost:5173")
        login_url = f"{frontend_url}/login"
        subject = f"[WEEG] Your agent account has been created"

        text_content = (
            f"Hello {agent.first_name},\n\n"
            f"Your WEEG agent account has been created by {created_by.full_name}.\n"
            f"Email: {agent.email}\n"
            f"Temporary password: {temporary_password}\n\n"
            f"Log in here: {login_url}\n"
            f"IMPORTANT: Change your password on first login.\n\n"
            f"Best regards,\nThe WEEG team"
        )

        html_content = f"""
        <!DOCTYPE html>
        <html>
        <body style="font-family:Arial,sans-serif;background:#f4f4f4;margin:0;padding:20px;">
          <div style="max-width:600px;margin:0 auto;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.1);">
            <div style="background:linear-gradient(135deg,#4f46e5,#7c3aed);padding:30px;text-align:center;">
              <h1 style="color:#fff;margin:0;">WEEG</h1>
            </div>
            <div style="padding:30px;">
              <h2 style="color:#1e293b;"> Welcome to WEEG!</h2>
              <p style="color:#475569;">Hello <strong>{agent.first_name}</strong>,</p>
              <p style="color:#475569;">Your agent account has been created by <strong>{created_by.full_name}</strong> ({company_name}).</p>

              <div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:8px;padding:20px;margin:20px 0;">
                <p style="color:#15803d;font-weight:bold;margin:0 0 12px;">Your login credentials:</p>
                <table style="width:100%;">
                  <tr>
                    <td style="color:#64748b;padding:4px 0;"> Email:</td>
                    <td style="color:#1e293b;font-weight:bold;">{agent.email}</td>
                  </tr>
                  <tr>
                    <td style="color:#64748b;padding:4px 0;"> Password:</td>
                    <td style="color:#1e293b;font-weight:bold;font-family:monospace;font-size:16px;">{temporary_password}</td>
                  </tr>
                </table>
              </div>

              <div style="background:#fffbeb;border:1px solid #fcd34d;border-radius:8px;padding:12px;margin:16px 0;">
                <p style="color:#92400e;margin:0;"> <strong>Important:</strong> You will need to change this temporary password on your first login.</p>
              </div>

              <div style="text-align:center;margin:30px 0;">
                <a href="{login_url}"
                   style="display:inline-block;background:linear-gradient(135deg,#4f46e5,#7c3aed);color:#fff;
                          text-decoration:none;padding:14px 32px;border-radius:8px;font-weight:bold;font-size:16px;">
                  Log in to WEEG
                </a>
              </div>
            </div>
            <div style="background:#f8fafc;padding:20px;text-align:center;border-top:1px solid #e2e8f0;">
              <p style="color:#94a3b8;font-size:12px;margin:0;">Automatic WEEG email — Do not reply.</p>
            </div>
          </div>
        </body>
        </html>
        """

        msg = EmailMultiAlternatives(subject, text_content, settings.DEFAULT_FROM_EMAIL, [agent.email])
        msg.attach_alternative(html_content, "text/html")
        msg.send(fail_silently=False)

    @staticmethod
    def send_password_reset_link(user: User, reset_token: str) -> None:
        frontend_url = getattr(settings, "FRONTEND_URL", "http://localhost:5173")
        reset_link = f"{frontend_url}/reset-password?token={reset_token}"
        subject = "[WEEG] Password Reset Request"

        text_content = (
            f"Hello {user.first_name},\n\n"
            f"Click here to reset your password:\n{reset_link}\n\n"
            f"This link is valid for 1 hour.\n\nBest regards,\nThe WEEG team"
        )

        html_content = f"""
        <!DOCTYPE html>
        <html>
        <body style="font-family:Arial,sans-serif;background:#f4f4f4;margin:0;padding:20px;">
          <div style="max-width:600px;margin:0 auto;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.1);">
            <div style="background:linear-gradient(135deg,#4f46e5,#7c3aed);padding:30px;text-align:center;">
              <h1 style="color:#fff;margin:0;">WEEG</h1>
            </div>
            <div style="padding:30px;">
              <h2 style="color:#1e293b;"> Password Reset</h2>
              <p style="color:#475569;">Hello <strong>{user.first_name}</strong>,</p>
              <p style="color:#475569;">A password reset request has been made for your account.</p>
              <div style="text-align:center;margin:30px 0;">
                <a href="{reset_link}"
                   style="display:inline-block;background:linear-gradient(135deg,#4f46e5,#7c3aed);color:#fff;
                          text-decoration:none;padding:14px 32px;border-radius:8px;font-weight:bold;font-size:16px;">
                  Reset My Password
                </a>
              </div>
              <p style="color:#94a3b8;font-size:13px;text-align:center;"> This link expires in <strong>1 hour</strong>.</p>
              <p style="color:#94a3b8;font-size:13px;text-align:center;">If you did not request this reset, please ignore this email.</p>
            </div>
            <div style="background:#f8fafc;padding:20px;text-align:center;border-top:1px solid #e2e8f0;">
              <p style="color:#94a3b8;font-size:12px;margin:0;">Automatic WEEG email — Do not reply.</p>
            </div>
          </div>
        </body>
        </html>
        """

        msg = EmailMultiAlternatives(subject, text_content, settings.DEFAULT_FROM_EMAIL, [user.email])
        msg.attach_alternative(html_content, "text/html")
        msg.send(fail_silently=False)

    @staticmethod
    def send_password_changed_confirmation(user: User) -> None:
        subject = "[WEEG] Your password has been changed"

        text_content = (
            f"Hello {user.first_name},\n\n"
            f"Your password has been successfully changed.\n"
            f"If this was not you, please contact your administrator immediately.\n\n"
            f"Best regards,\nThe WEEG team"
        )

        html_content = f"""
        <!DOCTYPE html>
        <html>
        <body style="font-family:Arial,sans-serif;background:#f4f4f4;margin:0;padding:20px;">
          <div style="max-width:600px;margin:0 auto;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.1);">
            <div style="background:linear-gradient(135deg,#4f46e5,#7c3aed);padding:30px;text-align:center;">
              <h1 style="color:#fff;margin:0;">WEEG</h1>
            </div>
            <div style="padding:30px;">
              <h2 style="color:#16a34a;"> Password Changed</h2>
              <p style="color:#475569;">Hello <strong>{user.first_name}</strong>,</p>
              <p style="color:#475569;">Your password has been successfully changed.</p>
              <div style="background:#fef2f2;border:1px solid #fecaca;border-radius:8px;padding:12px;margin:16px 0;">
                <p style="color:#dc2626;margin:0;"> If you are not the one who made this change, contact your administrator immediately.</p>
              </div>
            </div>
            <div style="background:#f8fafc;padding:20px;text-align:center;border-top:1px solid #e2e8f0;">
              <p style="color:#94a3b8;font-size:12px;margin:0;">Automatic WEEG email — Do not reply.</p>
            </div>
          </div>
        </body>
        </html>
        """

        msg = EmailMultiAlternatives(subject, text_content, settings.DEFAULT_FROM_EMAIL, [user.email])
        msg.attach_alternative(html_content, "text/html")
        msg.send(fail_silently=False)

    # ── ADD this method to EmailService ────────────────────────────────────────
    @staticmethod
    def send_email_verification(manager, token: str) -> None:
        frontend_url = getattr(settings, "FRONTEND_URL", "http://localhost:5173").rstrip('/')
        verify_url = f"{frontend_url}/signup/verify-email?token={token}"

        subject = "[WEEG] Please verify your email address"

        text_content = (
            f"Hello {manager.first_name},\n\n"
            f"Thank you for creating your WEEG Manager account.\n\n"
            f"Please verify your email address by clicking the link below:\n"
            f"{verify_url}\n\n"
            f"This link expires in 24 hours.\n\n"
            f"If you did not create this account, you can safely ignore this email.\n\n"
            f"Best regards,\nThe WEEG team"
        )

        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head><meta charset="UTF-8"></head>
        <body style="font-family:Arial,sans-serif;background:#f4f4f4;margin:0;padding:20px;">
          <div style="max-width:600px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,0.08);">
            <div style="background:linear-gradient(135deg,#4f46e5,#7c3aed);padding:32px 40px;text-align:center;">
              <div style="display:inline-block;background:rgba(255,255,255,0.15);border-radius:12px;padding:12px 20px;margin-bottom:16px;">
                <span style="color:#fff;font-size:28px;font-weight:900;letter-spacing:2px;">WEEG</span>
              </div>
              <div style="width:56px;height:56px;background:rgba(255,255,255,0.2);border-radius:50%;margin:0 auto 12px;line-height:56px;text-align:center;">
                <span style="font-size:26px;">✉️</span>
              </div>
              <h1 style="color:#fff;margin:0;font-size:22px;font-weight:600;">Verify your email address</h1>
            </div>
            <div style="padding:40px;">
              <p style="color:#374151;font-size:16px;margin:0 0 8px;">Hello <strong>{manager.first_name}</strong>,</p>
              <p style="color:#6b7280;font-size:15px;margin:0 0 28px;line-height:1.6;">
                Thank you for registering on <strong>WEEG</strong>.<br>
                To complete your registration, please verify your email address by clicking the button below.
              </p>
              <div style="text-align:center;margin:32px 0;">
                <a href="{verify_url}"
                  style="display:inline-block;background:linear-gradient(135deg,#4f46e5,#7c3aed);
                          color:#fff;font-size:16px;font-weight:700;
                          padding:16px 40px;border-radius:10px;text-decoration:none;">
                  ✉️ Verify my email address
                </a>
              </div>
              <div style="background:#fffbeb;border:1px solid #fcd34d;border-radius:8px;padding:12px 16px;margin:24px 0;text-align:center;">
                <p style="color:#92400e;margin:0;font-size:13px;">
                  ⏰ This link expires in <strong>24 hours</strong>.
                </p>
              </div>
              <div style="background:#f0f9ff;border:1px solid #bae6fd;border-radius:10px;padding:20px 24px;margin-bottom:24px;">
                <p style="color:#0369a1;font-size:14px;font-weight:600;margin:0 0 12px;">What happens next?</p>
                <table style="width:100%;border-collapse:collapse;">
                  <tr><td style="padding:5px 0;color:#0369a1;font-size:13px;">1. Click the button above to verify your email</td></tr>
                  <tr><td style="padding:5px 0;color:#0369a1;font-size:13px;">2. An administrator will review your account request</td></tr>
                  <tr><td style="padding:5px 0;color:#0369a1;font-size:13px;">3. You will receive a confirmation email once approved</td></tr>
                </table>
              </div>
              <p style="color:#9ca3af;font-size:12px;text-align:center;margin:0;">
                If you did not create this account, please ignore this email.<br>
                You can also copy this link: <a href="{verify_url}" style="color:#4f46e5;">{verify_url}</a>
              </p>
            </div>
            <div style="background:#f9fafb;border-top:1px solid #e5e7eb;padding:20px 40px;text-align:center;">
              <p style="color:#9ca3af;font-size:12px;margin:0;">
                WEEG — Financial Analytics & System Intelligence<br>
                This is an automated email, please do not reply.
              </p>
            </div>
          </div>
        </body>
        </html>
        """

        msg = EmailMultiAlternatives(subject, text_content, settings.DEFAULT_FROM_EMAIL, [manager.email])
        msg.attach_alternative(html_content, "text/html")
        msg.send(fail_silently=False)
        logger.info(f"[EMAIL VERIFY] Verification email sent to {manager.email}")
class UserService:
    @staticmethod
    def approve_manager(manager: User, admin: User) -> None:
        manager.approve()
        EmailService.send_manager_approved(manager)
        logger.info(f"Manager [{manager.email}] approved by [{admin.email}].")

    @staticmethod
    def reject_manager(manager: User, admin: User, reason: str) -> None:
        manager.reject(reason=reason)
        EmailService.send_manager_rejected(manager=manager, reason=reason)
        logger.info(f"Manager [{manager.email}] rejected by [{admin.email}]. Reason: {reason}.")

    @staticmethod
    def create_agent(validated_data: dict, manager: User, temporary_password: str) -> User:
        validated_data.pop("temporary_password", None)
        agent = User(
            role=User.Role.AGENT,
            status=User.AccountStatus.ACTIVE,
            is_verified=True,
            must_change_password=True,
            created_by=manager,
            company=manager.company,
            **validated_data,
        )
        agent.set_password(temporary_password)
        agent.save()
        EmailService.send_agent_credentials(
            agent=agent,
            temporary_password=temporary_password,
            created_by=manager,
        )
        logger.info(f"Agent [{agent.email}] created by [{manager.email}].")
        return agent

    @staticmethod
    def change_password(user: User, new_password: str, request) -> None:
        from apps.token_security.services import TokenService
        user.set_password(new_password)
        user.must_change_password = False
        user.save(update_fields=["password", "must_change_password", "updated_at"])
        user.increment_token_version()
        TokenService.revoke_all_user_tokens(user=user, reason="password_changed")
        EmailService.send_password_changed_confirmation(user=user)
        logger.info(f"Password changed for [{user.email}].")

    @staticmethod
    def reset_password(user: User, new_password: str) -> None:
        from apps.token_security.services import TokenService
        user.set_password(new_password)
        user.save(update_fields=["password", "updated_at"])
        user.increment_token_version()
        TokenService.revoke_all_user_tokens(user=user, reason="password_reset")
        EmailService.send_password_changed_confirmation(user=user)
        logger.info(f"Password reset for [{user.email}].")

    @staticmethod
    def request_password_reset(target_user: User, requesting_user: User) -> None:
        from apps.token_security.services import TokenService
        reset_token = TokenService.issue_temporary_token(user=target_user, action="password_reset")
        EmailService.send_password_reset_link(user=target_user, reset_token=reset_token)
        logger.info(f"Reset link sent to [{target_user.email}] by [{requesting_user.email}].")