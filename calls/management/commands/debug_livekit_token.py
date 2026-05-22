import jwt
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from calls.livekit import generate_join_token, livekit_identity
from calls.models import CallSession


class Command(BaseCommand):
    help = "Generate and inspect a LiveKit join token for an accepted/active call without printing secrets."

    def add_arguments(self, parser):
        parser.add_argument("call_id", type=int)
        parser.add_argument("--user-id", type=int)

    def handle(self, *args, **options):
        call = CallSession.objects.select_related("caller", "receiver").get(id=options["call_id"])
        if call.status not in {CallSession.Status.ACCEPTED, CallSession.Status.ACTIVE}:
            raise CommandError(f"Call status must be accepted or active, got {call.status}.")

        user_id = options.get("user_id") or call.caller_id
        User = get_user_model()
        user = User.objects.get(id=user_id)
        if user.id not in {call.caller_id, call.receiver_id}:
            raise CommandError("User must be caller or receiver.")

        token = generate_join_token(user, call)
        claims = jwt.decode(token, options={"verify_signature": False})

        self.stdout.write(f"server_url={settings.LIVEKIT_URL}")
        self.stdout.write(f"call_id={call.id}")
        self.stdout.write(f"room_name={call.room_name}")
        self.stdout.write(f"user_id={user.id}")
        self.stdout.write(f"identity={livekit_identity(user)}")
        self.stdout.write(f"token_length={len(token)}")
        self.stdout.write(f"claim_sub={claims.get('sub')}")
        self.stdout.write(f"claim_name={claims.get('name')}")
        self.stdout.write(f"claim_video={claims.get('video')}")
        self.stdout.write(f"claim_exp={claims.get('exp')}")
        self.stdout.write(f"api_key_present={bool(settings.LIVEKIT_API_KEY)}")
        self.stdout.write(f"api_secret_present={bool(settings.LIVEKIT_API_SECRET)}")
