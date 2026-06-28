import { NavLink, Outlet } from "react-router-dom";
import { LayoutDashboard, Users, Network, Settings, ScrollText, Gift, Banknote, MessageSquare } from "lucide-react";
import { cn } from "@/lib/utils";

const nav = [
  { to: "/", label: "Dashboard", icon: LayoutDashboard, end: true },
  { to: "/accounts", label: "Accounts", icon: Users },
  { to: "/proxies", label: "Proxys", icon: Network },
  { to: "/redeem", label: "Einlösen", icon: Gift },
  { to: "/chat-redeem", label: "Chat-Einlösen", icon: MessageSquare },
  { to: "/heist", label: "Heist", icon: Banknote },
  { to: "/logs", label: "Logs", icon: ScrollText },
  { to: "/settings", label: "Einstellungen", icon: Settings },
];

export default function Layout() {
  return (
    <div className="flex min-h-screen">
      <aside className="hidden w-56 shrink-0 border-r border-zinc-800 p-4 sm:block">
        <div className="mb-6 flex items-center gap-2 px-2">
          <span className="text-xl">🟣</span>
          <span className="font-semibold">Miner Manager</span>
        </div>
        <nav className="space-y-1">
          {nav.map((n) => (
            <NavLink
              key={n.to}
              to={n.to}
              end={n.end}
              className={({ isActive }) =>
                cn(
                  "flex items-center gap-2 rounded-md px-3 py-2 text-sm",
                  isActive ? "bg-brand text-white" : "text-zinc-400 hover:bg-zinc-800"
                )
              }
            >
              <n.icon size={16} />
              {n.label}
            </NavLink>
          ))}
        </nav>
      </aside>

      {/* Mobile top nav */}
      <div className="flex flex-1 flex-col">
        <nav className="flex gap-1 overflow-x-auto border-b border-zinc-800 p-2 sm:hidden">
          {nav.map((n) => (
            <NavLink
              key={n.to}
              to={n.to}
              end={n.end}
              className={({ isActive }) =>
                cn(
                  "flex items-center gap-1.5 whitespace-nowrap rounded-md px-3 py-1.5 text-sm",
                  isActive ? "bg-brand text-white" : "text-zinc-400"
                )
              }
            >
              <n.icon size={15} />
              {n.label}
            </NavLink>
          ))}
        </nav>
        <main className="flex-1 p-4 sm:p-6">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
