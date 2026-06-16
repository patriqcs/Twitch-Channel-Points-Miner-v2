import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Save } from "lucide-react";
import { api } from "@/lib/api";
import { Button, Card, Textarea } from "@/components/ui";

export default function Settings() {
  const { data } = useQuery({ queryKey: ["streamers"], queryFn: api.getStreamers });
  const [raw, setRaw] = useState("");
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    if (data) setRaw(data.raw);
  }, [data]);

  const count = raw
    .split("\n")
    .map((l) => l.trim())
    .filter((l) => l && !l.startsWith("#")).length;

  return (
    <div className="space-y-5">
      <h1 className="text-2xl font-bold">Einstellungen</h1>

      <Card className="space-y-3">
        <div>
          <div className="font-semibold">Streamer-Liste</div>
          <div className="text-sm text-zinc-400">
            Wird von <b>allen</b> Accounts gemeinsam geschaut. Ein Streamer pro Zeile,
            <code className="px-1">#</code> für Kommentare. ({count} aktiv)
          </div>
        </div>
        <Textarea
          rows={12}
          value={raw}
          onChange={(e) => { setRaw(e.target.value); setSaved(false); }}
          placeholder={"streamer1\nstreamer2\nstreamer3"}
        />
        <div className="flex items-center gap-3">
          <Button
            onClick={async () => {
              await api.putStreamers(raw);
              setSaved(true);
            }}
          >
            <Save size={15} /> Speichern
          </Button>
          {saved && <span className="text-sm text-emerald-400">✅ Gespeichert</span>}
          <span className="text-xs text-zinc-500">
            Änderungen greifen beim nächsten Account-Neustart.
          </span>
        </div>
      </Card>
    </div>
  );
}
