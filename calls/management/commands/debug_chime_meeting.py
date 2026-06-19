from django.core.management.base import BaseCommand, CommandError
from django.contrib.auth import get_user_model
from calls.models import CallSession, CallAttendee
from calls.chime import build_join_response


class Command(BaseCommand):
    help = "Generate and inspect Chime meeting join credentials for a call session."

    def add_arguments(self, parser):
        parser.add_argument("call_id", type=int)
        parser.add_argument("--user-id", type=int, help="Optional user ID to generate join token for")

    def handle(self, *args, **options):
        try:
            call = CallSession.objects.select_related("caller", "receiver").get(id=options["call_id"])
        except CallSession.DoesNotExist:
            raise CommandError(f"CallSession with ID {options['call_id']} does not exist.")

        if call.status not in {CallSession.Status.ACCEPTED, CallSession.Status.ACTIVE}:
            raise CommandError(f"Call status must be accepted or active, got {call.status}.")

        user_id = options.get("user_id") or call.caller_id
        User = get_user_model()
        try:
            user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            raise CommandError(f"User with ID {user_id} does not exist.")

        if user.id not in {call.caller_id, call.receiver_id}:
            raise CommandError("User must be caller or receiver of the call.")

        try:
            payload = build_join_response(call, user)
            self.stdout.write(self.style.SUCCESS("Successfully generated Chime join credentials:"))
            self.stdout.write(f"Call ID: {payload['call_id']}")
            self.stdout.write(f"Provider: {payload['provider']}")
            self.stdout.write(f"Meeting ID: {payload['meeting']['MeetingId']}")
            self.stdout.write(f"Attendee ID: {payload['attendee']['AttendeeId']}")
            self.stdout.write(f"External User ID: {payload['attendee']['ExternalUserId']}")
            self.stdout.write(f"Join Token length: {len(payload['attendee']['JoinToken'])}")
        except Exception as exc:
            raise CommandError(f"Failed to build join response: {exc}")
