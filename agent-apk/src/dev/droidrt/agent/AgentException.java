package dev.droidrt.agent;

/** An error whose message is sent back to the host verbatim. */
final class AgentException extends Exception {
    AgentException(String message) {
        super(message);
    }
}
