/**
 * 新对话入口：三张角色卡片（检索鼠 / 讲解鼠 / 整理鼠）。
 * persona 在会话创建后不可变，因此选择即创建会话。
 */
import PersonaAvatar from './PersonaAvatar';
import { PERSONA_META, PERSONA_ORDER } from './personaMeta';
import type { Persona } from '../../api/ai';
import { isPersonaAvailable } from '../../config/aiPersonaAvailability';

interface PersonaPickerProps {
  onPick: (persona: Persona) => void;
  disabled?: boolean;
  variant?: 'desktop' | 'mobile';
}

export default function PersonaPicker({ onPick, disabled = false, variant = 'desktop' }: PersonaPickerProps) {
  return (
    <div className={`persona-picker persona-picker--${variant}`} role="group" aria-label="选择 AI 角色">
      <div className="persona-picker__title">选择一个角色开始对话</div>
      <div className="persona-picker__grid">
        {PERSONA_ORDER.map((persona) => {
          const meta = PERSONA_META[persona];
          const available = isPersonaAvailable(persona);
          return (
            <button
              key={persona}
              type="button"
              className={available ? 'persona-card' : 'persona-card persona-card--unavailable'}
              data-testid={`persona-pick-${persona}`}
              aria-describedby={available ? undefined : 'persona-status-' + persona}
              disabled={disabled || !available}
              onClick={() => {
                if (available) onPick(persona);
              }}
            >
              <PersonaAvatar persona={persona} size={56} className="persona-card__avatar" />
              <span className="persona-card__label">{meta.label}</span>
              {!available && (
                <span id={'persona-status-' + persona} className="persona-card__status">
                  维护中
                </span>
              )}
              <span className="persona-card__tagline">{meta.tagline}</span>
            </button>
          );
        })}
      </div>
    </div>
  );
}
