# Voice Companion and App Controller PRD

## 1. Overview

This product is a browser-based voice agent that acts as both a conversational companion and a hands-free app controller. The agent should feel natural, responsive, and useful inside the web app, with voice as a first-class interaction mode rather than a chat widget bolted onto the interface.

The initial implementation will use a modular voice pipeline instead of a realtime speech-to-speech model. Pipecat will orchestrate the voice loop, Deepgram will provide streaming speech-to-text, Groq and/or OpenRouter will provide streaming LLM responses, and Cartesia will provide streaming text-to-speech.

## 2. Goals

- Enable natural spoken conversation in a browser app.
- Let users control core app functionality hands-free.
- Keep latency low enough that the interaction feels conversational.
- Support interruption and turn-taking.
- Keep provider keys secure on the server.
- Make the LLM aware of the current app screen and selected state.
- Start with a small, reliable command set before expanding.

## 3. Non-Goals

- Do not use OpenAI Realtime or another direct realtime speech-to-speech model for the initial version.
- Do not use LangChain as a required dependency.
- Do not make Pipecat responsible for application business logic.
- Do not implement always-listening mode in the first version.
- Do not build complex long-term memory in the first version.
- Do not expose third-party API keys to the browser.

## 4. Target Users

Primary users are people who want a web app that can be operated conversationally, including users who prefer hands-free control, multitaskers, and users who benefit from spoken guidance while navigating or working inside the app.

The ideal experience should feel like talking to a helpful collaborator that can see the current app context and act on it safely.

## 5. Core Use Cases

### 5.1 Conversational Companion

The user can speak naturally with the agent for lightweight support, guidance, brainstorming, and in-session context.

Example prompts:

- "What are we working on?"
- "Talk me through this."
- "Help me decide what to do next."
- "Summarize what I just said."
- "Remember that I prefer short responses."

### 5.2 Hands-Free App Controller

The user can ask the agent to perform safe app actions.

Example prompts:

- "Open the tasks page."
- "Create a task called draft proposal."
- "Filter this list to active items."
- "Read what is on this screen."
- "Summarize the current view."
- "Move this item to tomorrow."

### 5.3 Screen-Aware Assistance

The agent receives a compact app state snapshot so it can understand the current view and available actions.

Example state:

```json
{
  "current_view": "tasks",
  "selected_item": "Draft proposal",
  "visible_items": ["Draft proposal", "Review budget", "Email Maya"],
  "available_actions": [
    "open_view",
    "create_task",
    "complete_task",
    "reschedule_task",
    "summarize_view"
  ]
}
```

## 6. Product Principles

- Voice should feel embedded in the app, not separate from it.
- The agent should use short, spoken-friendly responses by default.
- App actions should be explicit, typed, and auditable.
- Risky or destructive actions should require confirmation.
- The first version should optimize for a smooth voice loop before advanced memory or complex automation.
- The system should remain provider-flexible.

## 7. Recommended Architecture

```text
Browser app
  -> Pipecat transport/session
  -> Deepgram streaming STT
  -> Groq or OpenRouter streaming LLM
  -> Cartesia streaming TTS
  -> Browser audio output

Application backend
  -> API key storage
  -> session creation
  -> app action execution
  -> tool authorization
  -> logging and audit events
```

## 8. Technology Stack

### 8.1 Frontend

- React or Next.js
- Browser microphone capture
- Push-to-talk interaction for the first version
- Visual voice status indicator
- Transcript and event log for debugging
- App state snapshot publisher

### 8.2 Voice Pipeline

- Pipecat for orchestration
- Deepgram for streaming STT
- Cartesia for streaming TTS
- Groq for low-latency LLM responses
- OpenRouter for model experimentation and fallback

### 8.3 Backend

- Node.js, Python, or Next.js API routes depending on app stack
- Server-side environment variables for provider keys
- Tool execution layer
- Session logging
- Confirmation state for sensitive actions

Environment variables:

```env
DEEPGRAM_API_KEY=
CARTESIA_API_KEY=
GROQ_API_KEY=
OPENROUTER_API_KEY=
LLM_PROVIDER=groq
LLM_MODEL=
```

## 9. Functional Requirements

### 9.1 Voice Input

- The user can start a voice interaction from the browser.
- The app captures microphone audio with user permission.
- The system streams audio to the voice pipeline.
- The system shows when it is listening, thinking, and speaking.

### 9.2 Speech Recognition

- The system transcribes user speech using streaming STT.
- Partial transcripts may be used for responsiveness.
- Final transcripts are sent into the LLM turn.
- Transcripts are logged for debugging during development.

### 9.3 LLM Conversation

- The LLM receives the user transcript, system instructions, relevant session context, and current app state.
- The LLM responds in concise, natural spoken language.
- The LLM can decide whether to answer conversationally or call an app tool.
- The LLM should avoid long monologues unless asked.

### 9.4 Text-to-Speech

- The system streams the LLM response to TTS.
- Audio playback begins as soon as practical.
- The user can interrupt speech.
- TTS voice should be configurable.

### 9.5 App Control Tools

The first version should include a small set of safe tools:

- `open_view`
- `read_current_view`
- `summarize_current_view`
- `create_item`
- `search_items`
- `select_item`
- `update_item`

Riskier tools should require confirmation:

- `delete_item`
- `send_message`
- `submit_form`
- `purchase`
- `share_content`

### 9.6 App State Snapshot

The frontend should provide a compact state object to the agent. The snapshot should include:

- current view
- selected item
- visible item names or summaries
- available commands
- relevant form state
- user permissions when applicable

The snapshot should avoid sending unnecessary private or bulky data.

### 9.7 Confirmation Flow

For risky actions:

1. The agent explains the intended action.
2. The user confirms verbally.
3. The backend executes the action.
4. The agent reports the result.

### 9.8 Memory

Initial memory should be session-scoped only.

Optional lightweight memory:

- user name
- preferred response length
- preferred voice mode
- current project or task context

Long-term memory should be deferred until the voice loop and app control layer are stable.

## 10. Non-Functional Requirements

### 10.1 Latency

The product should prioritize low-latency streaming at every stage.

Targets:

- Speech start detection should feel immediate.
- Agent should begin responding quickly after user turn completion.
- TTS playback should start before the full response is generated when possible.
- Interruptions should stop active speech promptly.

### 10.2 Reliability

- Voice sessions should recover gracefully from provider failures.
- The app should show clear states when audio, STT, LLM, or TTS is unavailable.
- Tool calls should return structured success or error results.

### 10.3 Security

- Provider API keys must remain server-side.
- The browser should never receive Deepgram, Cartesia, Groq, or OpenRouter keys.
- Tool execution should validate arguments server-side.
- User permissions should be checked before actions run.

### 10.4 Privacy

- Microphone access must require explicit browser permission.
- The app should make listening state visible.
- Session logs should be configurable and removable.
- Sensitive screen data should not be sent to the LLM unless required.

## 11. UX Requirements

- Start with push-to-talk or click-to-talk.
- Show clear visual states: idle, listening, thinking, speaking, error.
- Include a compact transcript/event panel for development.
- Keep spoken responses short by default.
- Let users interrupt the agent.
- Provide visual confirmation when app actions are performed.
- Avoid making the user repeat context already visible on screen.

## 12. First Prototype Scope

The first prototype should prove the voice loop and basic app control.

Must include:

- Browser mic capture
- Pipecat voice session
- Deepgram STT
- Groq or OpenRouter LLM streaming
- Cartesia TTS
- Push-to-talk interaction
- Basic status indicator
- Transcript display
- App state snapshot
- Three to five app tools
- Confirmation for one risky action

Should not include yet:

- Always-listening mode
- Wake word
- Long-term memory
- Complex multi-agent workflows
- LangChain or LangGraph
- Phone call support

## 13. Suggested Build Phases

### Phase 1: Voice Loop

- Connect browser audio to Pipecat.
- Stream STT through Deepgram.
- Send final transcripts to the LLM.
- Stream responses to Cartesia.
- Play TTS audio in the browser.

### Phase 2: Conversational Behavior

- Add companion-style system instructions.
- Tune response length and tone.
- Add interruption handling.
- Add transcript and debug logging.

### Phase 3: App Awareness

- Send current app state snapshots to the LLM.
- Let the agent describe the current screen.
- Let the agent answer questions about visible content.

### Phase 4: App Control

- Add typed tool registry.
- Implement safe navigation and item creation tools.
- Add visual feedback for completed actions.
- Add confirmation for risky actions.

### Phase 5: Refinement

- Tune latency.
- Compare Groq and OpenRouter models.
- Tune Cartesia voice settings.
- Add lightweight session memory.
- Add provider fallback behavior.

## 14. Open Questions

- Which frontend framework will the app use?
- What is the first real app domain: tasks, notes, dashboard, CRM, or something else?
- Should the first interaction be push-to-talk, toggle-to-talk, or both?
- Which Groq and OpenRouter models should be tested first?
- Which Cartesia voice should be the default?
- What actions are considered risky in the first app?
- How much transcript logging is acceptable during development?

## 15. Success Metrics

- User can complete a spoken interaction without typing.
- Agent starts responding fast enough to feel conversational.
- User can interrupt the agent while it is speaking.
- Agent correctly understands the current app view.
- Agent successfully performs at least three app actions.
- Risky actions require confirmation before execution.
- No provider API keys are exposed to the browser.

## 16. Initial Recommendation

Build the first version with Pipecat, Deepgram, Cartesia, and Groq as the default low-latency path. Keep OpenRouter available as a model experimentation layer. Avoid LangChain until orchestration complexity justifies it.

The best first milestone is not a sophisticated memory system or a large command library. It is a voice loop that feels responsive, interruptible, and app-aware.
