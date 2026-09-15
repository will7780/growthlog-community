/**
 * 底部标签栏（移动端）— lucide 五列稳定布局
 */
import { NotebookPen, Lightbulb, Plus, CalendarCheck, Bot } from 'lucide-react';

export type TabType = 'entries' | 'insights' | 'todos' | 'ai';

interface BottomTabBarProps {
  activeTab: TabType;
  onTabChange: (tab: TabType) => void;
  onCreateClick?: () => void;
}

const TABS: Array<{
  key: TabType | 'create';
  label: string;
  icon: typeof NotebookPen;
}> = [
  { key: 'entries', label: '记录', icon: NotebookPen },
  { key: 'insights', label: '洞察', icon: Lightbulb },
  { key: 'create', label: '新建', icon: Plus },
  { key: 'todos', label: '小要事', icon: CalendarCheck },
  { key: 'ai', label: 'AI', icon: Bot },
];

export default function BottomTabBar({ activeTab, onTabChange, onCreateClick }: BottomTabBarProps) {
  return (
    <nav className="bottom-tab-bar" aria-label="移动端主导航">
      {TABS.map((tab) => {
        if (tab.key === 'create') {
          return (
            <button
              key="create"
              type="button"
              className="tab-item tab-create"
              onClick={onCreateClick}
              aria-label="新建记录"
              title="新建记录"
            >
              <span className="tab-create-fab">
                <Plus size={22} aria-hidden="true" strokeWidth={2.25} />
              </span>
              <span className="tab-label tab-label--create">新建</span>
            </button>
          );
        }

        const Icon = tab.icon;
        const active = activeTab === tab.key;
        return (
          <button
            key={tab.key}
            type="button"
            className={`tab-item${active ? ' active' : ''}`}
            onClick={() => onTabChange(tab.key as TabType)}
            aria-label={tab.label}
            aria-current={active ? 'page' : undefined}
            title={tab.label}
          >
            <Icon size={20} aria-hidden="true" strokeWidth={active ? 2.25 : 2} />
            <span className="tab-label">{tab.label}</span>
          </button>
        );
      })}
    </nav>
  );
}
