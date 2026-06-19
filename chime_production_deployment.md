# Amazon Chime SDK Production Deployment & Configuration Guide

This guide details the steps to deploy the newly migrated Amazon Chime SDK calling backend to the AWS EC2 production environment, fully replacing the legacy LiveKit installation.

---

## 1. AWS IAM Instance Profile Policy
Since AWS credentials must **never** be stored in environment variables (`.env`) or source code, the EC2 instance must obtain permissions via an IAM Role attached to it (IAM Instance Profile).

Attach the following policy to the IAM Role associated with your EC2 instance:

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "ChimeSDKMeetingsAccess",
            "Effect": "Allow",
            "Action": [
                "chime:CreateMeeting",
                "chime:DeleteMeeting",
                "chime:CreateAttendee",
                "chime:GetMeeting",
                "chime:ListMeetings"
            ],
            "Resource": "*"
        }
    ]
}
```

---

## 2. Server Environment Variables Configuration
Update your `.env` file on the production EC2 server. Remove all LiveKit variables and configure the Amazon Chime SDK variables:

```bash
# =========================
# AMAZON CHIME SDK MEETINGS
# =========================
CHIME_ENABLED=True
CHIME_MEDIA_REGION=ap-south-1
AWS_REGION=ap-south-1
AWS_DEFAULT_REGION=ap-south-1
```

---

## 3. Database Migration
Apply the migrations to update the production database schema. Run the following command on your EC2 instance:

```bash
python manage.py migrate
```

---

## 4. Install Dependencies
Update your Python dependencies on the server to install `boto3` and remove `livekit-api`:

```bash
pip install -r requirements.txt
```

---

## 5. Verifying Deployment
You can use the new Django administrative command to verify that Chime meeting provisioning works correctly:

```bash
python manage.py debug_chime_meeting <call_id>
```
