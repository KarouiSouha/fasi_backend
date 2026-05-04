# Email Verification Setup Guide

## Overview
This implementation adds email verification for manager signup to ensure that only valid, existing email addresses can be used during account creation. The system verifies emails using the Abstract Email Validation API.

## Implementation Details

### 1. **Email Verification Service** (`apps/authentication/email_verification.py`)
- Validates email addresses exist and are deliverable
- Uses Abstract Email Validation API for verification
- Gracefully handles API failures (allows signup if service is unavailable)
- Checks format, validity, and deliverability

### 2. **Manager Signup Process**
When a manager attempts to sign up:
1. Email format is validated
2. Email is checked against existing database
3. **NEW**: Email is verified using Abstract API to ensure it exists
4. Manager account is created with PENDING status
5. Admin receives notification for approval

### 3. **Configuration Required**

#### Step 1: Get Abstract API Key
1. Visit: https://www.abstractapi.com/api/email-validation
2. Sign up for a free account
3. Create or select the **Email Validation** product
4. Copy your API key

> Important: use the key from the **Email Validation** product, not the Email Reputation product.

#### Step 2: Add to Backend Environment
Add to your `.env` file:
```
ABSTRACT_API_KEY=your_api_key_here
```

Or in production, set as environment variable on your hosting platform.

#### Step 3: Install Dependencies
Ensure `requests` is installed (already added to requirements):
```bash
pip install requests>=2.31.0
```

### 4. **Email Verification Behavior**

#### Valid Cases:
- ✅ Email exists and can receive messages → Account created
- ✅ No API key configured → Account created (verification disabled)
- ✅ API service unavailable → Account created (fallback)

#### Invalid Cases:
- ❌ Email format is invalid → Error: "Please provide a valid email address format"
- ❌ Email doesn't exist → Error: "The email address provided does not exist or is invalid"
- ❌ Email can't receive messages → Error: "The email address cannot receive messages"
- ❌ Email already registered in system → Error: "This email address is already in use"

### 5. **API Free Tier Limits**
- 100 requests/month on free tier
- Upgrade for production use if needed

### 6. **Error Messages Shown to Users**

The system provides clear error messages:
- Invalid format: "Please provide a valid email address format."
- Non-existent: "The email address provided does not exist or is invalid. Please verify and try again with a valid email address."
- Not deliverable: "The email address cannot receive messages. Please use a valid, active email address."

### 7. **Logging**
All verification attempts are logged in Django logs for debugging:
- ✅ Successful verifications
- ❌ Failed verifications
- API errors and timeouts

## Testing

### With API Key:
```bash
# Valid, existing email
curl -X POST http://localhost:8000/api/auth/manager-signup/ \
  -H "Content-Type: application/json" \
  -d '{
    "email": "user@gmail.com",
    "first_name": "John",
    "last_name": "Doe",
    "phone_number": "+1234567890",
    "password": "SecurePass123!",
    "password_confirm": "SecurePass123!",
    "company_name": "Acme Corp",
    "industry": "Retail",
    "country": "USA",
    "city": "New York",
    "current_erp": "SAP"
  }'

# Invalid/non-existent email
curl -X POST http://localhost:8000/api/auth/manager-signup/ \
  -H "Content-Type: application/json" \
  -d '{
    "email": "nonexistent.fake.email.12345@test.com",
    ...
  }'
```

### Without API Key:
If no API key is configured, email verification is skipped and accounts are created normally.

## Technical Details

### Email Verification API Response
```json
{
  "is_valid_format": true,
  "deliverability": "DELIVERABLE",
  "is_smtp_valid": true,
  "quality_score": 0.95,
  ...
}
```

### Verification Logic
1. **Format Check**: Validates email pattern (must have @ and . in domain)
2. **API Call**: Calls Abstract API to verify existence and deliverability
3. **Result Evaluation**: 
   - Email must have valid format
   - Email must be DELIVERABLE or RISKY (not UNDELIVERABLE)
   - SMTP must be valid

## Files Modified

1. `apps/authentication/email_verification.py` - NEW service file
2. `apps/authentication/serializers.py` - Updated ManagerSignupSerializer
3. `config/settings/base.py` - Added ABSTRACT_API_KEY setting
4. `.env` - Added ABSTRACT_API_KEY variable
5. `.env.example` - Added ABSTRACT_API_KEY documentation
6. `requirements/base.txt` - Added requests library

## Future Enhancements

- Add Google Workspace API support for enterprise users
- Implement rate limiting on verification attempts
- Add email confirmation step before account activation
- Support multiple email verification providers
- Add webhook support for real-time email validation

## Support

For issues with Abstract API:
- Documentation: https://www.abstractapi.com/api/email-validation
- Status page: https://www.abstractapi.com/status

For application issues:
- Check Django logs in `logs/django.log`
- Verify ABSTRACT_API_KEY is set correctly
- Test API key directly on Abstract API website
