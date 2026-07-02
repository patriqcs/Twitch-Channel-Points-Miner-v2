import { Routes, Route } from "react-router-dom";
import Layout from "./components/Layout";
import Dashboard from "./pages/Dashboard";
import Accounts from "./pages/Accounts";
import Proxies from "./pages/Proxies";
import Redeem from "./pages/Redeem";
import ChatRedeem from "./pages/ChatRedeem";
import WebRedeem from "./pages/WebRedeem";
import Heist from "./pages/Heist";
import Settings from "./pages/Settings";
import Logs from "./pages/Logs";

export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Dashboard />} />
        <Route path="accounts" element={<Accounts />} />
        <Route path="proxies" element={<Proxies />} />
        <Route path="redeem" element={<Redeem />} />
        <Route path="chat-redeem" element={<ChatRedeem />} />
        <Route path="web-redeem" element={<WebRedeem />} />
        <Route path="heist" element={<Heist />} />
        <Route path="logs" element={<Logs />} />
        <Route path="settings" element={<Settings />} />
      </Route>
    </Routes>
  );
}
