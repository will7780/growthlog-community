/**
 * 可复用图标按钮 — tooltip + aria-label + focus-visible
 */
import type { ButtonHTMLAttributes } from 'react';
import type { LucideIcon } from 'lucide-react';

interface IconButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  icon: LucideIcon;
  /** 用于 aria-label 与 title tooltip */
  label: string;
  size?: 'sm' | 'md';
  variant?: 'default' | 'ghost' | 'accent';
}

export default function IconButton({
  icon: Icon,
  label,
  size = 'md',
  variant = 'default',
  className = '',
  type = 'button',
  ...props
}: IconButtonProps) {
  const sizeClass = size === 'sm' ? 'icon-button--sm' : 'icon-button--md';
  const variantClass =
    variant === 'ghost' ? 'icon-button--ghost' : variant === 'accent' ? 'icon-button--accent' : '';

  return (
    <button
      type={type}
      className={`icon-button ${sizeClass} ${variantClass} ${className}`.trim()}
      aria-label={label}
      title={label}
      {...props}
    >
      <Icon aria-hidden="true" size={size === 'sm' ? 16 : 18} strokeWidth={2} />
    </button>
  );
}
