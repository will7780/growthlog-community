/**
 * 输入框组件
 */
import type { InputHTMLAttributes } from 'react';

interface InputProps extends InputHTMLAttributes<HTMLInputElement> {
  label?: string;
  error?: string;
}

export default function Input({ label, error, className = '', id, ...props }: InputProps) {
  const inputId = id || (label ? `input-${label}` : undefined);

  return (
    <div className={`mb-4 ${className.includes('mb-0') ? '!mb-0' : ''}`}>
      {label && (
        <label htmlFor={inputId} className="block text-sm font-medium text-gray-700 mb-1">
          {label}
        </label>
      )}
      <input
        id={inputId}
        className={`w-full px-3 py-2 border rounded-md text-base min-h-[44px] wb-focus-visible ${
          error ? 'border-[var(--wb-status-error)]' : 'border-[var(--wb-border)]'
        } ${className}`}
        style={{ letterSpacing: 'var(--wb-letter-spacing)' }}
        {...props}
      />
      {error && <p className="mt-1 text-sm text-[var(--wb-status-error)]">{error}</p>}
    </div>
  );
}
