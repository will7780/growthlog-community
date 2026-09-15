/**
 * 骨架屏组件
 */
interface SkeletonProps {
  className?: string;
}

function SkeletonBlock({ className = '' }: SkeletonProps) {
  return (
    <div className={`animate-pulse bg-gray-200 rounded ${className}`} />
  );
}

export function EntryCardSkeleton() {
  return (
    <div className="bg-white rounded-lg shadow p-4 mb-4">
      <SkeletonBlock className="h-5 w-1/3 mb-3" />
      <SkeletonBlock className="h-3 w-1/4 mb-2" />
      <SkeletonBlock className="h-4 w-full mb-2" />
      <SkeletonBlock className="h-4 w-5/6 mb-2" />
      <SkeletonBlock className="h-4 w-2/3 mb-4" />
      <div className="flex items-center justify-between pt-3 border-t border-gray-100">
        <SkeletonBlock className="h-4 w-24" />
        <SkeletonBlock className="h-6 w-12 rounded" />
      </div>
    </div>
  );
}

export function EntryListSkeleton({ count = 3 }: { count?: number }) {
  return (
    <>
      {Array.from({ length: count }).map((_, i) => (
        <EntryCardSkeleton key={i} />
      ))}
    </>
  );
}
