/**
 * 按钮组件
 */
import type { ButtonHTMLAttributes } from 'react';

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: 'primary' | 'secondary';
  loading?: boolean;
}

export default function Button({
  variant = 'primary',
  loading = false,
  children,
  disabled,
  className = '',
  ...props
}: ButtonProps) {
  const baseStyles =
    'px-4 py-2 rounded-md font-medium transition-colors min-h-[44px] wb-focus-visible letter-spacing-normal';
  const variantStyles = {
    primary:
      'bg-[var(--wb-accent)] text-white hover:bg-[var(--wb-accent-hover)] disabled:bg-[var(--wb-status-neutral)] disabled:text-[var(--wb-text-muted)]',
    secondary:
      'bg-[var(--wb-surface-muted)] text-[var(--wb-text)] hover:bg-[var(--wb-border)] disabled:bg-[var(--wb-surface-muted)] disabled:text-[var(--wb-text-muted)] border border-[var(--wb-border)]',
  };

  return (
    <button
      className={`${baseStyles} ${variantStyles[variant]} ${className}`}
      disabled={disabled || loading}
      style={{ letterSpacing: 'var(--wb-letter-spacing)' }}
      {...props}
    >
      {loading ? '加载中...' : children}
    </button>
  );
}
