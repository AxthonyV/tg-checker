import asyncio
import csv
import os
import re
import sys
import json
import time
from dataclasses import dataclass
from typing import Optional, Tuple

from telethon import TelegramClient
from telethon.errors import UsernameInvalidError, UsernameNotOccupiedError
from telethon.tl import functions, types

from colorama import Fore, Style, init as colorama_init

colorama_init(autoreset=True)

try:
	from dotenv import load_dotenv  # type: ignore
	load_dotenv()
except Exception:
	pass


INVITE_RE = re.compile(r"(?:https?://)?t\.me/(?:joinchat/|\+)([A-Za-z0-9_-]{16,})|tg://join\?invite=([A-Za-z0-9_-]{16,})", re.IGNORECASE)
USERNAME_RE = re.compile(r"(?:https?://)?t\.me/([A-Za-z0-9_]{5,32})|@([A-Za-z0-9_]{5,32})", re.IGNORECASE)


@dataclass
class CheckResult:
	input_value: str
	status: str
	kind: str
	visibility: str
	verified: Optional[bool]
	requires_approval: Optional[bool]
	member_count: Optional[int]
	title: Optional[str]
	username: Optional[str]
	extra: Optional[str]


def parse_input(value: str) -> Tuple[str, Optional[str]]:
	value = value.strip()
	if not value:
		return ("unknown", None)
	m_inv = INVITE_RE.search(value)
	if m_inv:
		code = m_inv.group(1) or m_inv.group(2)
		return ("invite", code)
	m_user = USERNAME_RE.search(value)
	if m_user:
		username = m_user.group(1) or m_user.group(2)
		return ("username", username)
	return ("unknown", None)


def classify_chat(entity: types.User | types.Chat | types.Channel) -> Tuple[str, str, Optional[bool]]:
	if isinstance(entity, types.Channel):
		verified = bool(getattr(entity, "verified", False))
		kind = "supergroup" if entity.megagroup else "channel"
		visibility = "public" if entity.username else "private"
		return kind, visibility, verified
	elif isinstance(entity, types.Chat):
		return "group", "private", bool(getattr(entity, "verified", False))
	elif isinstance(entity, types.User):
		return "user", "public" if entity.username else "private", bool(getattr(entity, "verified", False))
	return "unknown", "unknown", None


async def check_invite(client: TelegramClient, code: str) -> CheckResult:
	try:
		res = await client(functions.messages.CheckChatInviteRequest(hash=code))
		requires_approval = None
		title = None
		member_count = None
		verified = None
		username = None
		kind = "unknown"
		visibility = "private"
		extra = None
		if isinstance(res, types.ChatInvite):
			requires_approval = bool(getattr(res, "request_needed", False))
			title = res.title
			member_count = getattr(res, "participants_count", None)
			verified = bool(getattr(res, "verified", False))
			kind = "supergroup" if res.channel and res.megagroup else ("channel" if res.channel else "group")
		elif isinstance(res, types.ChatInviteAlready):
			entity = res.chat
			kind, visibility, verified = classify_chat(entity)
			username = getattr(entity, "username", None)
			member_count = None
		return CheckResult(
			input_value=code,
			status="valid",
			kind=kind,
			visibility=visibility,
			verified=verified,
			requires_approval=requires_approval,
			member_count=member_count,
			title=title,
			username=username,
			extra=extra,
		)
	except Exception as e:
		return CheckResult(
			input_value=code,
			status=f"invalid: {type(e).__name__}",
			kind="unknown",
			visibility="unknown",
			verified=None,
			requires_approval=None,
			member_count=None,
			title=None,
			username=None,
			extra=str(e),
		)


async def check_username(client: TelegramClient, username: str) -> CheckResult:
	try:
		resolved = await client(functions.contacts.ResolveUsernameRequest(username=username))
		entity = None
		if resolved.chats:
			entity = resolved.chats[0]
		elif resolved.users:
			entity = resolved.users[0]
		if entity is None:
			raise UsernameNotOccupiedError(request=None)
		kind, visibility, verified = classify_chat(entity)
		requires_approval = None
		member_count = None
		if isinstance(entity, types.Channel):
			try:
				full = await client(functions.channels.GetFullChannelRequest(channel=entity))
				if full and full.full_chat:
					member_count = getattr(full.full_chat, "participants_count", None)
					join_requests = getattr(full.full_chat, "requests_pending", None)
					requires_approval = True if (join_requests is not None and join_requests > 0) else None
			except Exception:
				pass
		return CheckResult(
			input_value=username,
			status="resolved",
			kind=kind,
			visibility=visibility,
			verified=verified,
			requires_approval=requires_approval,
			member_count=member_count,
			title=getattr(entity, "title", None),
			username=getattr(entity, "username", username),
			extra=None,
		)
	except (UsernameInvalidError, UsernameNotOccupiedError) as e:
		return CheckResult(
			input_value=username,
			status=f"invalid_username: {type(e).__name__}",
			kind="unknown",
			visibility="unknown",
			verified=None,
			requires_approval=None,
			member_count=None,
			title=None,
			username=None,
			extra=None,
		)
	except Exception as e:
		return CheckResult(
			input_value=username,
			status=f"error: {type(e).__name__}",
			kind="unknown",
			visibility="unknown",
			verified=None,
			requires_approval=None,
			member_count=None,
			title=None,
			username=None,
			extra=str(e),
		)


def normalize_output(value: Optional[str | int | bool]) -> str:
	if value is None:
		return ""
	return str(value)


def _english_kind(kind: str) -> str:
	return {
		"supergroup": "Supergroup",
		"channel": "Channel",
		"group": "Group",
		"user": "User",
	}.get(kind, "Unknown")


def _english_visibility(visibility: str) -> str:
	return {
		"public": "Public",
		"private": "Private",
	}.get(visibility, "Unknown")


def _format_compact_en(res: CheckResult) -> str:
	parts: list[str] = []
	parts.append(f"type: {_english_kind(res.kind)}")
	parts.append(f"visibility: {_english_visibility(res.visibility)}")
	if res.member_count is not None:
		parts.append(f"members: {res.member_count}")
	if res.requires_approval is not None:
		parts.append(f"approval: {'Yes' if res.requires_approval else 'No'}")
	if res.verified is not None:
		parts.append(f"verified: {'Yes' if res.verified else 'No'}")
	if res.username:
		parts.append(f"username: @{res.username}")
	return " | ".join(parts)


def _english_reason(status: str, extra: Optional[str]) -> str:
	text = f"{status} {extra or ''}".lower()
	mapping = [
		("invitehashexpired", "invite expired"),
		("invitehashinvalid", "invalid invite"),
		("invitehashempty", "invalid invite"),
		("usernamenotoccupied", "username not found"),
		("usernameinvalid", "invalid username"),
		("channelprivate", "private chat"),
		("chatadminrequired", "admin rights required"),
		("floodwait", "rate limit, try later"),
		("authkeypermanentlyinvalid", "invalid session, sign in again"),
	]
	for needle, reason in mapping:
		if needle in text:
			return reason
	return "error"


def _format_minimal_en(res: CheckResult) -> str:
	base = f"{_english_kind(res.kind)} {_english_visibility(res.visibility)}"
	tokens: list[str] = []
	if res.verified:
		tokens.append("+verified")
	if res.requires_approval:
		tokens.append("+approval")
	if res.username:
		tokens.append(f"+@{res.username}")
	if res.member_count is not None:
		tokens.append(f"m={res.member_count}")
	return base + (" " + " ".join(tokens) if tokens else "")


async def run(input_path: str, output_path: str | None) -> None:
	api_id = os.getenv("API_ID")
	api_hash = os.getenv("API_HASH")
	session_name = os.getenv("SESSION_NAME", "bulk_checker")
	if not api_id or not api_hash:
		print("Missing API_ID/API_HASH in .env or environment variables", file=sys.stderr)
		sys.exit(1)

	output_mode = os.getenv("OUTPUT_MODE", "compact").lower()
	use_color = not bool(os.getenv("NO_COLOR")) and sys.stdout.isatty()
	def tag(text: str, color: str) -> str:
		return f"{color}{text}{Style.RESET_ALL}" if use_color else text

	client = TelegramClient(session_name, int(api_id), api_hash)
	await client.connect()
	if not await client.is_user_authorized():
		await client.start()

	processed = 0
	errors = 0
	results: list[CheckResult] = []
	with open(input_path, newline='', encoding='utf-8') as f:
		reader = csv.DictReader(f)
		fieldnames = reader.fieldnames or []
		values: list[str] = []
		if 'input' in fieldnames:
			for row in reader:
				value = (row.get('input') or '').strip()
				if value:
					values.append(value)
		else:
			f.seek(0)
			for row in csv.reader(f):
				if not row:
					continue
				value = (row[0] or '').strip()
				if value and value.lower() != 'input':
					values.append(value)

	for value in values:
		type_, data = parse_input(value)
		try:
			if type_ == 'invite' and data:
				res = await check_invite(client, data)
				res.input_value = value
			elif type_ == 'username' and data:
				res = await check_username(client, data)
				res.input_value = value
			else:
				res = CheckResult(
					input_value=value,
					status='unrecognized',
					kind='unknown',
					visibility='unknown',
					verified=None,
					requires_approval=None,
					member_count=None,
					title=None,
					username=None,
					extra=None,
				)
			results.append(res)
			processed += 1
			if output_mode == 'jsonl':
				print(json.dumps({
					"input": res.input_value,
					"status": res.status,
					"kind": res.kind,
					"visibility": res.visibility,
					"verified": res.verified,
					"requires_approval": res.requires_approval,
					"member_count": res.member_count,
					"title": res.title,
					"username": res.username,
					"extra": res.extra,
				}))
			else:
				if res.status in ("valid", "resolved"):
					label = tag("[VALID]", Fore.GREEN)
					body = _format_minimal_en(res) if output_mode == 'minimal' else _format_compact_en(res)
					print(f"{label} {value} -> {body}")
				elif res.status == 'unrecognized':
					label = tag("[UNKNOWN]", Fore.YELLOW)
					print(f"{label} {value}")
				else:
					label = tag("[INVALID]", Fore.RED)
					reason = _english_reason(res.status, res.extra)
					print(f"{label} {value} -> {reason}")
		except Exception as e:
			errors += 1
			label = tag("[ERROR]", Fore.RED)
			print(f"{label} {value} -> {type(e).__name__}: {e}")

	ok = sum(1 for r in results if r.status in ("valid", "resolved"))
	unrec = sum(1 for r in results if r.status == 'unrecognized')
	bad = processed - ok - unrec
	print()
	summary_label = tag("Summary", Fore.CYAN)
	print(f"{summary_label}: processed={processed}  ok={ok}  unknown={unrec}  errors={errors}  invalid={bad}")

	if output_path:
		only_valid = os.getenv("CSV_ONLY_VALID", "1") != "0"
		rows = [r for r in results if (not only_valid) or (r.status in ("valid", "resolved"))]
		with open(output_path, 'w', newline='', encoding='utf-8') as f:
			writer = csv.writer(f)
			headers = [
				"input", "kind", "visibility", "member_count", "verified", "username", "requires_approval", "title"
			]
			writer.writerow(headers)
			for r in rows:
				writer.writerow([
					r.input_value,
					r.kind,
					r.visibility,
					normalize_output(r.member_count),
					normalize_output(r.verified),
					normalize_output(r.username),
					normalize_output(r.requires_approval),
					normalize_output(r.title),
				])

	await client.disconnect()


def main() -> None:
	title = os.getenv("WINDOW_TITLE")
	try:
		if title:
			if os.name == 'nt':
				os.system(f"title {title}")
			else:
				sys.stdout.write(f"\33]0;{title}\a")
				sys.stdout.flush()
	except Exception:
		pass
	clear_flag_env = os.getenv("CLEAR_ON_START", "0").lower()
	if clear_flag_env in ("1", "true", "yes"):
		os.system("cls" if os.name == 'nt' else "clear")

	input_path = os.getenv("INPUT_FILE", "inputs.csv")
	output_env = os.getenv("OUTPUT_FILE", "results.csv")
	output_path: str | None = output_env if output_env != "" else None
	asyncio.run(run(input_path, output_path))

	try:
		hold_seconds = int(os.getenv("WAIT_BEFORE_EXIT_SECONDS", "0"))
	except Exception:
		hold_seconds = 0
	if hold_seconds > 0:
		time.sleep(hold_seconds)


if __name__ == "__main__":
	main()
