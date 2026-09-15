import type { TodoResponse, TodoTreeNode } from '../../types/todo';

export function flattenTodoTree(nodes: TodoTreeNode[]): TodoResponse[] {
  const out: TodoResponse[] = [];
  const visit = (node: TodoTreeNode) => {
    const { children: _children, ...todo } = node;
    out.push(todo);
    node.children.forEach(visit);
  };
  nodes.forEach(visit);
  return out;
}

export function filterTodoTree(
  nodes: TodoTreeNode[],
  include: (todo: TodoTreeNode) => boolean,
): TodoTreeNode[] {
  const visit = (node: TodoTreeNode): TodoTreeNode[] => {
    const children = node.children.flatMap(visit);
    if (!include(node)) return children;
    return [{ ...node, children }];
  };
  return nodes.flatMap(visit);
}

export function todoTreeIds(nodes: TodoTreeNode[]): Set<number> {
  return new Set(flattenTodoTree(nodes).map((todo) => todo.id));
}