/**
 * 附件相关 API
 */
import { del, downloadBlob, get, post, postForm } from './client';
import type { AttachmentResponse } from '../types/api';

export async function uploadEntryAttachment(
  entryId: number,
  file: File
): Promise<AttachmentResponse> {
  const formData = new FormData();
  formData.append('file', file);
  return postForm<AttachmentResponse>(`/api/entries/${entryId}/attachments`, formData, {
    timeoutMs: 60000,
  });
}

export async function getEntryAttachments(entryId: number): Promise<AttachmentResponse[]> {
  return get<AttachmentResponse[]>(`/api/entries/${entryId}/attachments`);
}

export async function getAttachmentStatus(attachmentId: number): Promise<AttachmentResponse> {
  return get<AttachmentResponse>(`/api/attachments/${attachmentId}/status`);
}

export async function reprocessAttachment(
  attachmentId: number,
): Promise<AttachmentResponse> {
  return post<AttachmentResponse>(`/api/attachments/${attachmentId}/reprocess`);
}

export async function deleteAttachment(attachmentId: number): Promise<void> {
  await del<void>(`/api/attachments/${attachmentId}`);
}

export async function downloadAttachment(attachment: AttachmentResponse): Promise<void> {
  const blob = await downloadBlob(`/api/attachments/${attachment.id}/download`, {
    timeoutMs: 60000,
  });
  const url = window.URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = attachment.original_filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.URL.revokeObjectURL(url);
}

interface AttachmentImageRequestOptions {
  signal?: AbortSignal;
}

export async function getAttachmentThumbnailBlob(
  attachmentId: number,
  options?: AttachmentImageRequestOptions,
): Promise<Blob> {
  return downloadBlob(`/api/attachments/${attachmentId}/thumbnail`, {
    timeoutMs: 30000,
    cache: 'default',
    signal: options?.signal,
  });
}

export async function getAttachmentPreviewBlob(
  attachmentId: number,
  options?: AttachmentImageRequestOptions,
): Promise<Blob> {
  return downloadBlob(`/api/attachments/${attachmentId}/preview`, {
    timeoutMs: 60000,
    cache: 'default',
    signal: options?.signal,
  });
}