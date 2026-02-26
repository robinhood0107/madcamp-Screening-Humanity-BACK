from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthPrincipal
from app.core.config import settings
from app.models.audio import AudioFile
from app.models.chat_message import ChatMessage
from app.models.conversation import Conversation
from app.models.conversation_audio_asset import ConversationAudioAsset

logger = logging.getLogger(__name__)


@dataclass
class OwnerRef:
    kind: str
    user_id: Optional[str] = None
    guest_session_id: Optional[str] = None


def _owner_from_principal(principal: Optional[AuthPrincipal]) -> Optional[OwnerRef]:
    if principal is None:
        return None
    if principal.kind == "user" and principal.user:
        return OwnerRef(kind="user", user_id=principal.user.id)
    if principal.kind == "guest" and principal.guest_session:
        return OwnerRef(kind="guest", guest_session_id=principal.guest_session.id)
    return None


def _owner_filter(owner: OwnerRef):
    if owner.kind == "user" and owner.user_id:
        return and_(
            Conversation.owner_user_id == owner.user_id,
            Conversation.owner_guest_session_id.is_(None),
        )
    if owner.kind == "guest" and owner.guest_session_id:
        return and_(
            Conversation.owner_guest_session_id == owner.guest_session_id,
            Conversation.owner_user_id.is_(None),
        )
    # impossible path but keeps type-safe expression usage
    return Conversation.id == "__never__"


def _audio_owner_filter(owner: OwnerRef):
    if owner.kind == "user" and owner.user_id:
        return and_(
            ConversationAudioAsset.owner_user_id == owner.user_id,
            ConversationAudioAsset.owner_guest_session_id.is_(None),
        )
    if owner.kind == "guest" and owner.guest_session_id:
        return and_(
            ConversationAudioAsset.owner_guest_session_id == owner.guest_session_id,
            ConversationAudioAsset.owner_user_id.is_(None),
        )
    return ConversationAudioAsset.id == "__never__"


def _derive_mode_from_request(request: Any) -> str:
    persona = getattr(request, "persona", None) or ""
    if "[배우 1:" in persona and "[배우 2:" in persona:
        return "director"
    return "actor"


def _derive_title_from_request(request: Any, assistant_content: str, speaker_name: Optional[str]) -> str:
    scenario = getattr(request, "scenario", None) or {}
    opponent = (scenario.get("opponent") or "").strip()
    situation = (scenario.get("situation") or "").strip()
    if opponent and situation:
        return f"{opponent} · {situation[:80]}"
    if opponent:
        return opponent[:120]
    if speaker_name:
        return f"{speaker_name}와의 대화"
    text = (assistant_content or "").strip()
    if not text:
        return "대화 기록"
    line = text.replace("\n", " ").strip()
    return (line[:120] + "...") if len(line) > 120 else line


def _guess_mime_type_from_url(file_url: Optional[str]) -> Optional[str]:
    if not file_url:
        return None
    lower = file_url.lower()
    if lower.endswith(".wav"):
        return "audio/wav"
    if lower.endswith(".ogg"):
        return "audio/ogg"
    if lower.endswith(".aac"):
        return "audio/aac"
    if lower.endswith(".mp3"):
        return "audio/mpeg"
    return None


def _resolve_asset_file_path_from_url(file_url: Optional[str]) -> Optional[str]:
    if not file_url or not file_url.startswith("/assets/"):
        return None
    rel = file_url[len("/assets/") :].lstrip("/")
    # StaticFiles("/assets") -> USER_ASSETS_DIR
    return str(Path(settings.USER_ASSETS_DIR) / rel)


def _ensure_owner_compatible(conversation: Conversation, owner: OwnerRef) -> None:
    if owner.kind == "user":
        if conversation.owner_guest_session_id and conversation.owner_guest_session_id != "":
            raise PermissionError("guest-owned conversation cannot be reused by user")
        if conversation.owner_user_id and conversation.owner_user_id != owner.user_id:
            raise PermissionError("conversation belongs to another user")
    else:
        if conversation.owner_user_id and conversation.owner_user_id != "":
            raise PermissionError("user-owned conversation cannot be reused by guest")
        if conversation.owner_guest_session_id and conversation.owner_guest_session_id != owner.guest_session_id:
            raise PermissionError("conversation belongs to another guest session")


def _safe_existing_file_under_user_assets(path_str: Optional[str]) -> Optional[Path]:
    """
    [역할]
    다운로드 대상 파일 경로가 USER_ASSETS_DIR 내부의 실제 파일인지 검사한다.

    [주의]
    DB 메타가 오염되었을 때 임의 경로 다운로드로 이어지지 않도록
    user assets 루트 바깥 경로는 거부한다.
    """
    if not path_str:
        return None
    try:
        path = Path(path_str).expanduser().resolve()
        root = Path(settings.USER_ASSETS_DIR).resolve()
        path.relative_to(root)
    except Exception:
        return None
    if not path.is_file():
        return None
    return path


async def ensure_conversation_for_session(
    *,
    db: AsyncSession,
    session_id: str,
    principal: Optional[AuthPrincipal],
    request: Any,
    create_if_missing: bool = True,
) -> Optional[Conversation]:
    """
    [역할]
    인증된 주체(user/guest)에 한해 legacy session_id에 대응하는 conversation 루트를 생성/검증한다.

    [주의]
    - 비로그인(anonymous) 요청은 아직 history 저장 범위에서 제외하므로 None 반환.
    - 동일 session_id 재사용 시 소유권 충돌을 검사해 타인 기록 오염을 막는다.
    """
    owner = _owner_from_principal(principal)
    if owner is None:
        return None

    result = await db.execute(select(Conversation).where(Conversation.legacy_session_id == session_id))
    conversation = result.scalar_one_or_none()
    if conversation:
        _ensure_owner_compatible(conversation, owner)
        if conversation.deleted_at is not None:
            # 삭제된 기록에 다시 append하는 경우 재활성화하지 않고 새 session_id를 쓰는 것이 안전하다.
            raise PermissionError("deleted conversation session_id cannot be reused")
        return conversation

    if not create_if_missing:
        return None

    conversation = Conversation(
        legacy_session_id=session_id,
        owner_user_id=owner.user_id,
        owner_guest_session_id=owner.guest_session_id,
        mode=_derive_mode_from_request(request),
        status="active",
    )
    db.add(conversation)
    await db.flush()
    await db.commit()
    await db.refresh(conversation)
    return conversation


async def update_conversation_after_chat_turn(
    *,
    db: AsyncSession,
    session_id: str,
    principal: Optional[AuthPrincipal],
    request: Any,
    assistant_content: str,
    speaker_name: Optional[str],
) -> Optional[Conversation]:
    owner = _owner_from_principal(principal)
    if owner is None:
        return None

    result = await db.execute(select(Conversation).where(Conversation.legacy_session_id == session_id))
    conversation = result.scalar_one_or_none()
    if conversation is None:
        conversation = await ensure_conversation_for_session(
            db=db,
            session_id=session_id,
            principal=principal,
            request=request,
        )
        if conversation is None:
            return None
    else:
        _ensure_owner_compatible(conversation, owner)

    counts_result = await db.execute(
        select(func.count(ChatMessage.id)).where(
            ChatMessage.session_id == session_id,
            ChatMessage.role == "assistant",
        )
    )
    assistant_turn_count = counts_result.scalar_one() or 0

    conversation.mode = _derive_mode_from_request(request)
    if not conversation.title:
        conversation.title = _derive_title_from_request(request, assistant_content, speaker_name)
    conversation.character_name = speaker_name or conversation.character_name
    conversation.preview_text = (assistant_content or "")[:500] if assistant_content else conversation.preview_text
    conversation.turn_count = int(assistant_turn_count)
    conversation.last_message_at = datetime.utcnow()
    conversation.status = "active"

    await db.commit()
    await db.refresh(conversation)
    return conversation


async def attach_conversation_audio_asset_from_chat_tts(
    *,
    db: AsyncSession,
    principal: Optional[AuthPrincipal],
    conversation_id: str,
    message_id: Optional[str],
    tts_data: dict[str, Any],
    fallback_voice_id: Optional[str] = None,
) -> Optional[ConversationAudioAsset]:
    owner = _owner_from_principal(principal)
    if owner is None:
        return None

    audio_url = tts_data.get("audio_url")
    if not audio_url:
        return None

    file_path = _resolve_asset_file_path_from_url(audio_url)
    size_bytes: Optional[int] = None
    if file_path:
        try:
            size_bytes = Path(file_path).stat().st_size
        except FileNotFoundError:
            logger.info("History audio file path missing during attach: %s", file_path)

    audio_file_id: Optional[str] = None
    if owner.kind == "user":
        result = await db.execute(select(AudioFile).where(AudioFile.file_url == audio_url))
        audio_row = result.scalar_one_or_none()
        if audio_row:
            audio_file_id = audio_row.id
            if size_bytes is None:
                size_bytes = audio_row.file_size

    asset = ConversationAudioAsset(
        conversation_id=conversation_id,
        message_id=message_id,
        owner_user_id=owner.user_id,
        owner_guest_session_id=owner.guest_session_id,
        audio_file_id=audio_file_id,
        file_id=tts_data.get("file_id"),
        file_url=audio_url,
        file_path=file_path,
        voice_id=tts_data.get("voice_id") or fallback_voice_id,
        duration_sec=tts_data.get("duration"),
        mime_type=_guess_mime_type_from_url(audio_url),
        size_bytes=size_bytes,
    )
    db.add(asset)

    conv_result = await db.execute(select(Conversation).where(Conversation.id == conversation_id))
    conversation = conv_result.scalar_one_or_none()
    if conversation:
        conversation.audio_count = int((conversation.audio_count or 0) + 1)

    await db.commit()
    await db.refresh(asset)
    return asset


async def list_history_items(
    *,
    db: AsyncSession,
    principal: AuthPrincipal,
    status_filter: Optional[str],
    mode: Optional[str],
    has_audio: Optional[bool],
    limit: int,
    search: Optional[str],
) -> dict[str, Any]:
    owner = _owner_from_principal(principal)
    if owner is None:
        return {"items": [], "next_cursor": None, "total": 0}

    clauses = [_owner_filter(owner)]
    clauses.append(Conversation.deleted_at.is_(None))
    clauses.append(Conversation.status != "deleted_soft")
    if status_filter:
        clauses.append(Conversation.status == status_filter)
    if mode:
        clauses.append(Conversation.mode == mode)
    if has_audio is True:
        clauses.append(Conversation.audio_count > 0)
    elif has_audio is False:
        clauses.append(or_(Conversation.audio_count.is_(None), Conversation.audio_count == 0))
    if search:
        like = f"%{search.strip()}%"
        clauses.append(
            or_(
                Conversation.title.ilike(like),
                Conversation.preview_text.ilike(like),
                Conversation.character_name.ilike(like),
            )
        )

    total_q = select(func.count(Conversation.id)).where(*clauses)
    total = (await db.execute(total_q)).scalar_one() or 0

    rows = (
        await db.execute(
            select(Conversation)
            .where(*clauses)
            .order_by(Conversation.last_message_at.desc(), Conversation.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()

    items = [
        {
            "id": row.id,
            "session_id": row.legacy_session_id,
            "title": row.title,
            "status": row.status,
            "mode": row.mode,
            "turn_count": row.turn_count,
            "audio_count": row.audio_count,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "character_name": row.character_name,
            "thumbnail_url": None,
            "preview_text": row.preview_text,
        }
        for row in rows
    ]
    return {"items": items, "next_cursor": None, "total": int(total)}


async def get_history_detail_for_owner(
    *,
    db: AsyncSession,
    principal: AuthPrincipal,
    conversation_id: str,
) -> Optional[dict[str, Any]]:
    owner = _owner_from_principal(principal)
    if owner is None:
        return None

    conversation = (
        await db.execute(
            select(Conversation).where(
                Conversation.id == conversation_id,
                _owner_filter(owner),
                Conversation.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if not conversation:
        return None

    messages = (
        await db.execute(
            select(ChatMessage)
            .where(ChatMessage.session_id == conversation.legacy_session_id)
            .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
        )
    ).scalars().all()

    audio_assets = (
        await db.execute(
            select(ConversationAudioAsset)
            .where(
                ConversationAudioAsset.conversation_id == conversation.id,
                ConversationAudioAsset.deleted_at.is_(None),
            )
            .order_by(ConversationAudioAsset.created_at.asc())
        )
    ).scalars().all()

    return {
        "conversation": {
            "id": conversation.id,
            "session_id": conversation.legacy_session_id,
            "title": conversation.title,
            "status": conversation.status,
            "mode": conversation.mode,
            "character_name": conversation.character_name,
            "preview_text": conversation.preview_text,
            "created_at": conversation.created_at.isoformat() if conversation.created_at else None,
            "updated_at": conversation.updated_at.isoformat() if conversation.updated_at else None,
            "scenario_snapshot": None,
            "generated_script": None,
        },
        "messages": [
            {
                "id": m.id,
                "role": m.role,
                "content": m.content,
                "speaker_name": m.character_name,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in messages
        ],
        "audio_assets": [
            {
                "id": a.id,
                "message_id": a.message_id,
                "voice_id": a.voice_id,
                "duration_sec": a.duration_sec,
                "download_url": a.file_url,
                "play_url": a.file_url,
                "mime_type": a.mime_type,
                "size_bytes": a.size_bytes,
            }
            for a in audio_assets
        ],
    }


async def list_history_audio_assets_for_owner(
    *,
    db: AsyncSession,
    principal: AuthPrincipal,
    conversation_id: str,
    audio_ids: Optional[list[str]] = None,
    message_ids: Optional[list[str]] = None,
) -> Optional[dict[str, Any]]:
    """
    [역할]
    소유권 검증 후 대화의 다운로드 가능한 오디오 자산 목록을 조회한다.

    [반환]
    None: 대화가 없거나 접근 불가
    dict: conversation + assets(rows) + files(resolved path metadata)
    """
    owner = _owner_from_principal(principal)
    if owner is None:
        return None

    conversation = (
        await db.execute(
            select(Conversation).where(
                Conversation.id == conversation_id,
                _owner_filter(owner),
                Conversation.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if not conversation:
        return None

    clauses = [
        ConversationAudioAsset.conversation_id == conversation.id,
        ConversationAudioAsset.deleted_at.is_(None),
    ]
    if audio_ids:
        clauses.append(ConversationAudioAsset.id.in_(list(dict.fromkeys(audio_ids))))
    if message_ids:
        clauses.append(ConversationAudioAsset.message_id.in_(list(dict.fromkeys(message_ids))))

    rows = (
        await db.execute(
            select(ConversationAudioAsset)
            .where(*clauses)
            .order_by(ConversationAudioAsset.created_at.asc(), ConversationAudioAsset.id.asc())
        )
    ).scalars().all()

    downloadable_files: list[dict[str, Any]] = []
    for row in rows:
        resolved_path = _safe_existing_file_under_user_assets(row.file_path)

        if resolved_path is None and row.audio_file_id:
            audio_meta = (
                await db.execute(select(AudioFile).where(AudioFile.id == row.audio_file_id))
            ).scalar_one_or_none()
            if audio_meta:
                resolved_path = _safe_existing_file_under_user_assets(audio_meta.file_path)
                if not row.mime_type:
                    row.mime_type = (
                        f"audio/{audio_meta.format}"
                        if audio_meta.format and audio_meta.format != "mp3"
                        else ("audio/mpeg" if audio_meta.format == "mp3" else row.mime_type)
                    )
                if row.size_bytes is None:
                    row.size_bytes = audio_meta.file_size

        if resolved_path is None and row.file_url:
            resolved_path = _safe_existing_file_under_user_assets(_resolve_asset_file_path_from_url(row.file_url))

        if resolved_path is None:
            continue

        downloadable_files.append(
            {
                "asset": row,
                "path": resolved_path,
            }
        )

    return {
        "conversation": conversation,
        "assets": rows,
        "files": downloadable_files,
    }


async def resolve_history_tts_regenerate_target(
    *,
    db: AsyncSession,
    principal: AuthPrincipal,
    conversation_id: str,
    message_id: Optional[str],
    text: Optional[str],
    voice_id: Optional[str],
) -> Optional[dict[str, Any]]:
    """
    [역할]
    history TTS 재생성 요청에서 소유권 검증 + 텍스트/voice fallback 해석을 담당한다.

    [반환]
    - None: conversation 접근 불가
    - dict: conversation/message/text/voice_id
    """
    owner = _owner_from_principal(principal)
    if owner is None:
        return None

    conversation = (
        await db.execute(
            select(Conversation).where(
                Conversation.id == conversation_id,
                _owner_filter(owner),
                Conversation.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if not conversation:
        return None

    target_message: Optional[ChatMessage] = None
    if message_id:
        target_message = (
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.id == message_id,
                    ChatMessage.session_id == conversation.legacy_session_id,
                )
            )
        ).scalar_one_or_none()
        if not target_message:
            raise LookupError("대상 메시지를 찾을 수 없습니다.")

    resolved_text = (text or "").strip() or None
    if not resolved_text and target_message is not None:
        resolved_text = (target_message.content or "").strip() or None
    if not resolved_text:
        raise ValueError("재생성할 text 또는 유효한 message_id가 필요합니다.")

    resolved_voice_id = (voice_id or "").strip() or None
    if not resolved_voice_id:
        latest_q = (
            select(ConversationAudioAsset)
            .where(
                ConversationAudioAsset.conversation_id == conversation.id,
                ConversationAudioAsset.deleted_at.is_(None),
            )
            .order_by(ConversationAudioAsset.created_at.desc(), ConversationAudioAsset.id.desc())
        )
        if target_message is not None:
            latest_q = latest_q.where(ConversationAudioAsset.message_id == target_message.id)

        latest_asset = (await db.execute(latest_q.limit(1))).scalar_one_or_none()
        if latest_asset and latest_asset.voice_id:
            resolved_voice_id = latest_asset.voice_id
        elif target_message is not None:
            fallback_asset = (
                await db.execute(
                    select(ConversationAudioAsset)
                    .where(
                        ConversationAudioAsset.conversation_id == conversation.id,
                        ConversationAudioAsset.deleted_at.is_(None),
                        ConversationAudioAsset.voice_id.is_not(None),
                    )
                    .order_by(ConversationAudioAsset.created_at.desc(), ConversationAudioAsset.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if fallback_asset and fallback_asset.voice_id:
                resolved_voice_id = fallback_asset.voice_id

    return {
        "conversation": conversation,
        "message": target_message,
        "message_id": target_message.id if target_message else (message_id or None),
        "text": resolved_text,
        "voice_id": resolved_voice_id,
    }


async def soft_delete_history_for_owner(
    *,
    db: AsyncSession,
    principal: AuthPrincipal,
    conversation_id: str,
) -> Optional[dict[str, Any]]:
    owner = _owner_from_principal(principal)
    if owner is None:
        return None

    conversation = (
        await db.execute(
            select(Conversation).where(
                Conversation.id == conversation_id,
                _owner_filter(owner),
                Conversation.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if not conversation:
        return None

    deleted_audio_count = 0
    audio_rows = (
        await db.execute(
            select(ConversationAudioAsset).where(
                ConversationAudioAsset.conversation_id == conversation.id,
                ConversationAudioAsset.deleted_at.is_(None),
            )
        )
    ).scalars().all()
    now = datetime.utcnow()
    for row in audio_rows:
        row.deleted_at = now
        deleted_audio_count += 1
        # 파일 시스템 정리는 best-effort. 실패해도 history 삭제 자체는 진행한다.
        if row.file_path:
            try:
                p = Path(row.file_path)
                if p.exists():
                    p.unlink()
            except Exception:
                logger.exception("Failed to unlink history audio file row_id=%s path=%s", row.id, row.file_path)

    conversation.status = "deleted_soft"
    conversation.deleted_at = now
    conversation.updated_at = now

    await db.commit()
    return {
        "message": "기록이 삭제되었습니다.",
        "conversation_id": conversation.id,
        "deleted_audio_count": deleted_audio_count,
    }


async def guest_merge_preview_counts(
    *,
    db: AsyncSession,
    guest_session_id: str,
) -> dict[str, int]:
    conv_count = (
        await db.execute(
            select(func.count(Conversation.id)).where(
                Conversation.owner_guest_session_id == guest_session_id,
                Conversation.deleted_at.is_(None),
            )
        )
    ).scalar_one() or 0

    audio_count = (
        await db.execute(
            select(func.count(ConversationAudioAsset.id)).where(
                ConversationAudioAsset.owner_guest_session_id == guest_session_id,
                ConversationAudioAsset.deleted_at.is_(None),
            )
        )
    ).scalar_one() or 0

    storage_bytes = (
        await db.execute(
            select(func.coalesce(func.sum(ConversationAudioAsset.size_bytes), 0)).where(
                ConversationAudioAsset.owner_guest_session_id == guest_session_id,
                ConversationAudioAsset.deleted_at.is_(None),
            )
        )
    ).scalar_one() or 0

    return {
        "conversation_count": int(conv_count),
        "audio_count": int(audio_count),
        "storage_bytes": int(storage_bytes),
    }


async def move_guest_history_to_user(
    *,
    db: AsyncSession,
    guest_session_id: str,
    user_id: str,
) -> dict[str, int]:
    conv_rows = await db.execute(
        update(Conversation)
        .where(
            Conversation.owner_guest_session_id == guest_session_id,
            Conversation.deleted_at.is_(None),
        )
        .values(owner_user_id=user_id, owner_guest_session_id=None)
        .execution_options(synchronize_session=False)
    )
    audio_rows = await db.execute(
        update(ConversationAudioAsset)
        .where(
            ConversationAudioAsset.owner_guest_session_id == guest_session_id,
            ConversationAudioAsset.deleted_at.is_(None),
        )
        .values(owner_user_id=user_id, owner_guest_session_id=None)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return {
        "conversation_count": int(conv_rows.rowcount or 0),
        "audio_count": int(audio_rows.rowcount or 0),
    }
