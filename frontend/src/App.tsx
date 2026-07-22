import { useState } from "react";
import MeetingsPage from "./pages/MeetingsPage";
import ReviewPage from "./pages/ReviewPage";
import TemplatesPage from "./pages/TemplatesPage";
import ErrorBoundary from "./components/ErrorBoundary";

/** Three views — meetings list, review editor, templates — switched via two
 * clear top-bar tabs (no router needed). The review editor opens from a meeting
 * card and lives under the "Meetings" tab. */
export default function App() {
  const [openMeetingId, setOpenMeetingId] = useState<number | null>(null);
  const [showTemplates, setShowTemplates] = useState(false);

  const goMeetings = () => {
    setOpenMeetingId(null);
    setShowTemplates(false);
  };
  const goTemplates = () => {
    setOpenMeetingId(null);
    setShowTemplates(true);
  };

  return (
    <div className="app">
      <header className="topbar">
        <span
          className="brand"
          role="button"
          tabIndex={0}
          onClick={goMeetings}
          onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); goMeetings(); } }}
          title="Back to all meetings"
          aria-label="Meeting System — back to all meetings"
        >
          🎙️ Meeting System
        </span>
        <nav className="topnav" aria-label="Main navigation">
          <button
            className={`navtab ${!showTemplates ? "active" : ""}`}
            onClick={goMeetings}
            title="Upload and review your meetings"
          >
            📋 Meetings
          </button>
          <button
            className={`navtab ${showTemplates ? "active" : ""}`}
            onClick={goTemplates}
            title="Manage your Word export templates"
          >
            📄 Templates
          </button>
        </nav>
      </header>
      <ErrorBoundary key={showTemplates ? "templates" : openMeetingId ?? "meetings"}>
        {showTemplates ? (
          <TemplatesPage />
        ) : openMeetingId === null ? (
          <MeetingsPage onOpen={setOpenMeetingId} />
        ) : (
          <ReviewPage meetingId={openMeetingId} onBack={goMeetings} />
        )}
      </ErrorBoundary>
    </div>
  );
}
