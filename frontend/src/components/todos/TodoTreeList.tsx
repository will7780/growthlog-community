import { useState } from 'react';
import type { ButtonHTMLAttributes, CSSProperties } from 'react';
import {
  DndContext,
  KeyboardSensor,
  PointerSensor,
  TouchSensor,
  closestCenter,
  useSensor,
  useSensors,
  type DragEndEvent,
} from '@dnd-kit/core';
import {
  SortableContext,
  arrayMove,
  sortableKeyboardCoordinates,
  useSortable,
  verticalListSortingStrategy,
} from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
import type {
  TodoChildCreateRequest,
  TodoPriority,
  TodoTreeNode,
} from '../../types/todo';
import TodoItem from './TodoItem';

interface TodoTreeListProps {
  nodes: TodoTreeNode[];
  variant: 'today' | 'longterm';
  todayTodoIds: Set<number>;
  onToggle: (id: number, isDone: boolean, completionNote?: string | null) => Promise<void>;
  onUpdateDueDate: (id: number, dueDate: string | null) => Promise<void>;
  onUpdatePriority: (id: number, priority: TodoPriority) => Promise<void>;
  onUpdateUrgent: (id: number, isUrgent: boolean) => Promise<void>;
  onDelete: (id: number, expectedDescendantCount: number) => Promise<void>;
  onAddChild: (parentId: number, data: TodoChildCreateRequest) => Promise<unknown>;
  onReorderChildren: (parentId: number, orderedChildIds: number[]) => Promise<void>;
  onAddToToday?: (id: number) => Promise<void>;
  onRemoveFromToday?: (id: number) => Promise<void>;
}

interface SiblingListProps extends TodoTreeListProps {
  parentId: number | null;
  visibleDepth: number;
  collapsedIds: Set<number>;
  toggleCollapsed: (id: number) => void;
}

function SortableTodoNode({
  node,
  listProps,
}: {
  node: TodoTreeNode;
  listProps: SiblingListProps;
}) {
  const sortable = useSortable({
    id: node.id,
    disabled: listProps.parentId === null,
  });
  const hasChildren = node.children.length > 0;
  const collapsed = listProps.collapsedIds.has(node.id);
  const style = {
    '--todo-tree-depth': listProps.visibleDepth,
    transform: CSS.Transform.toString(sortable.transform),
    transition: sortable.transition,
  } as CSSProperties;
  const dragHandleProps = {
    ...sortable.attributes,
    ...sortable.listeners,
  } as ButtonHTMLAttributes<HTMLButtonElement>;

  return (
    <li
      ref={sortable.setNodeRef}
      className={'todo-tree__node' + (sortable.isDragging ? ' todo-tree__node--dragging' : '')}
      style={style}
    >
      <TodoItem
        todo={node}
        variant={listProps.variant}
        hasVisibleChildren={hasChildren}
        collapsed={collapsed}
        showAncestorPath={listProps.visibleDepth === 0 && node.depth > 0}
        dragHandleProps={listProps.parentId === null ? undefined : dragHandleProps}
        dragHandleRef={listProps.parentId === null ? undefined : sortable.setActivatorNodeRef}
        dragging={sortable.isDragging}
        onToggleCollapse={() => listProps.toggleCollapsed(node.id)}
        onToggle={listProps.onToggle}
        onUpdateDueDate={listProps.onUpdateDueDate}
        onUpdatePriority={listProps.onUpdatePriority}
        onUpdateUrgent={listProps.onUpdateUrgent}
        onDelete={listProps.onDelete}
        onAddChild={listProps.onAddChild}
        onAddToToday={listProps.onAddToToday}
        onRemoveFromToday={listProps.onRemoveFromToday}
      />
      {hasChildren && !collapsed ? (
        <TodoSiblingList
          {...listProps}
          nodes={node.children}
          parentId={node.id}
          visibleDepth={listProps.visibleDepth + 1}
        />
      ) : null}
    </li>
  );
}

function TodoSiblingList(props: SiblingListProps) {
  const [reorderError, setReorderError] = useState('');
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 6 } }),
    useSensor(TouchSensor, { activationConstraint: { delay: 180, tolerance: 8 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );
  const ids = props.nodes.map((node) => node.id);

  const handleDragEnd = (event: DragEndEvent) => {
    if (props.parentId === null || !event.over || event.active.id === event.over.id) return;
    const oldIndex = ids.indexOf(Number(event.active.id));
    const newIndex = ids.indexOf(Number(event.over.id));
    if (oldIndex < 0 || newIndex < 0) return;
    const orderedIds = arrayMove(ids, oldIndex, newIndex);
    setReorderError('');
    void props.onReorderChildren(props.parentId, orderedIds).catch((error) => {
      setReorderError(error instanceof Error ? error.message : '调整顺序失败，请稍后重试');
    });
  };

  const list = (
    <ul className={props.parentId === null ? 'todo-tree' : 'todo-tree__children'}>
      {props.nodes.map((node) => (
        <SortableTodoNode key={node.id} node={node} listProps={props} />
      ))}
      {reorderError ? <li className="todo-tree__order-error" role="alert">{reorderError}</li> : null}
    </ul>
  );

  if (props.parentId === null) return list;
  return (
    <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={handleDragEnd}>
      <SortableContext items={ids} strategy={verticalListSortingStrategy}>
        {list}
      </SortableContext>
    </DndContext>
  );
}

export default function TodoTreeList(props: TodoTreeListProps) {
  const [collapsedIds, setCollapsedIds] = useState<Set<number>>(new Set());

  const toggleCollapsed = (id: number) => {
    setCollapsedIds((previous) => {
      const next = new Set(previous);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  return (
    <TodoSiblingList
      {...props}
      parentId={null}
      visibleDepth={0}
      collapsedIds={collapsedIds}
      toggleCollapsed={toggleCollapsed}
    />
  );
}