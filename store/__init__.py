"""Хранилище: SQL-слой бота, разложенный по смыслу.

Раньше всё это лежало в одном файле repository.py на две тысячи строк.
Теперь каждый раздел отвечает за свой кусок базы, а общие хелперы живут
в store.common. Модули пакета опираются на store.common и, когда разделу
действительно нужен сосед, - на один прямой импорт из него: граф импортов
ацикличен, поэтому любой модуль можно импортировать и по отдельности.

Здесь перечислено всё содержимое разделов, поэтому `import store` даёт тот
же плоский интерфейс, что и `import repository`, а repository.py - тонкий
фасад поверх него.

Два имени - `groups` и `people` - совпадают с именами модулей пакета и
здесь, как и в repository, остаются функциями: их зовёт бот, а модули
доступны через `from store.groups import ...` и через sys.modules.
"""

from .access import (  # noqa: F401 - публичный интерфейс пакета
    attempts_count, attempts_log, clear_attempts, create_invite, create_staff_request,
    delete_invite, get_staff_request, invite_state, list_invites, note_attempt, set_invite_ttl,
    set_staff_request_status, staff_requests, use_invite,
)
from .analytics import (  # noqa: F401 - публичный интерфейс пакета
    audience_ids, broadcast_history, log_broadcast, response_speed, staff_load, stats_overview,
    students_by_group, tickets_by_category, tickets_by_day, tickets_by_status, top_groups,
)
from .common import (  # noqa: F401 - публичный интерфейс пакета
    ADMIN_ROLES_SQL, STAFF_ROLES_SQL, TOPIC_SOURCES, _CONTACT_SELECT, _code, _code_forms,
    _contact_filter, _parse_day, _row_dict, _row_value, canonical_group,
)
from .feedback import (  # noqa: F401 - публичный интерфейс пакета
    PUBLIC_FEEDBACK_KINDS, PUBLIC_FEEDBACK_STATUSES, count_public_feedback, get_public_feedback,
    list_public_feedback, prune_public_feedback_rates, set_public_feedback_status, submit_public_feedback,
)
from .groups import (  # noqa: F401 - публичный интерфейс пакета
    _group_dict, add_group_aliases, delete_group, find_group, get_group, group_aliases, groups,
    known_groups, list_groups, rename_group, resolve_group, set_group_active, suggest_groups,
    upsert_group,
)
from .panel import (  # noqa: F401 - публичный интерфейс пакета
    _duration_ru, admin_today, all_admins, all_settings, data_gaps, dedupe_stats,
    event_feed_label, list_users, list_users_count, recent_ticket_events,
)
from .people import (  # noqa: F401 - публичный интерфейс пакета
    CONTACT_KINDS, CONTACT_KIND_LABELS, KIND_TITLES, consent_of, contact_full_name,
    contact_kind, delete_user, get_user, give_consent, is_registered, people, people_count,
    people_overview, set_user_group, set_user_name, student_ids_by_name, student_open_tickets_count, touch_contact,
    upsert_user, user_card, users_without_consent,
)
from .schedules import (  # noqa: F401 - публичный интерфейс пакета
    all_parsed_groups, delete_schedule, delete_schedule_subscription, get_schedule,
    group_has_lessons, is_schedule_subscribed, lessons_count, lessons_for_group,
    lessons_for_teacher, reapply_lesson_times, save_lessons, schedule_groups, schedule_stamp,
    schedule_subscribers, search_teachers, set_lesson_time, set_schedule_subscription,
    stamp_is_fresh, stamp_matches_file, teacher_groups, upsert_schedule,
)
from .staff import (  # noqa: F401 - публичный интерфейс пакета
    ADMIN_FIELDS, _load_admins, add_staff, add_staff_many, admin_ids, clear_admin_fields,
    delete_staff, department_names, get_admin, list_staff, on_vacation, people_without_staff,
    people_without_staff_count, replacement_rule, set_admin_profile, set_staff_broadcast,
    set_staff_category, set_staff_see_all, set_vacation, staff_activity, staff_by_role,
    staff_for_category, staff_on_vacation, staff_sees_all, staff_sees_all_bulk, staff_targets,
    update_admin, vacation_note, vacation_replacement, vacations_bulk,
)
from .sysadmin import (  # noqa: F401 - публичный интерфейс пакета
    _save_revoked, add_sysadmin, admin_log, admin_log_counts, audit_actions, audit_count,
    audit_entries, grant_sysadmin, is_owner, is_sysadmin, list_sysadmins, log_action, log_audit,
    prune_audit, revoke_sysadmin, revoked_sysadmins, sysadmin_count,
)
from .templates import (  # noqa: F401 - публичный интерфейс пакета
    add_template, count_template_use, delete_template, get_template, list_templates,
    templates_count, update_template_text,
)
from .tickets import (  # noqa: F401 - публичный интерфейс пакета
    add_internal_note, add_ticket_message, admin_tickets, archive_count, archive_ticket,
    bulk_update, create_ticket, delete_ticket, forward_ticket, get_ticket, latest_message_roles, latest_message_states,
    log_ticket_event, open_tickets_count, recent_student_tickets, restore_ticket,
    set_ticket_ready, set_ticket_status, status_counts, ticket_events, ticket_thread,
    transition_ticket_status, update_ticket,
)
from .web_access import (  # noqa: F401 - публичные функции веб-доступа
    WEB_PERMISSIONS, WEB_WORK_PERMISSIONS, WEB_PERMISSION_PRESETS, get_staff_web_access,
    list_staff_web_access, set_staff_web_access, revoke_staff_web_sessions, staff_web_principal,
    staff_web_principal_in_tx, normalize_staff_web_permissions, normalize_staff_web_id,
    staff_web_secret, staff_web_hash, staff_web_session_hash, request_staff_web_code,
    verify_staff_web_code, cancel_staff_web_challenge, revoke_staff_web_session,
)
from .workspace import (  # noqa: F401 - единые права и операции рабочего места
    workspace_list, workspace_ticket, workspace_apply, workspace_attachment,
    workspace_template_preview, workspace_can_attach, workspace_attach_file,
)
