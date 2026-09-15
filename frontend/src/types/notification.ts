export type NotificationState =
  | 'unsubscribed'
  | 'web_push_enabled';

export interface NotificationSettings {
  state: NotificationState | string;
  reminders_enabled: boolean;
  today_plan_time?: string | null;
  unfinished_time?: string | null;
  urgent_overdue_enabled: boolean;
  quiet_hours_start?: string | null;
  quiet_hours_end?: string | null;
  privacy_note: string;
  web_push_ready?: boolean;
  active_device_count?: number;
  sound_note?: string | null;
}

export interface NotificationTestResponse {
  ok: boolean;
  message_id_present: boolean;
  cooldown_seconds: number;
}

export interface NotificationSettingsPatch {
  reminders_enabled?: boolean;
  today_plan_time?: string;
  unfinished_time?: string;
  urgent_overdue_enabled?: boolean;
  quiet_hours_start?: string | null;
  quiet_hours_end?: string | null;
}

export interface WebPushPublicKeyResponse {
  public_key: string;
  ready: boolean;
  enabled: boolean;
}

export interface WebPushOkResponse {
  ok: boolean;
  active_device_count: number;
}

export interface WebPushStatusResponse {
  current_device_active: boolean;
  active_device_count: number;
}
