// protocol.h — JSON protocol handling for the Chrono daemon.
//
// Uses nlohmann/json for parsing/serialization.
// Protocol: newline-delimited JSON (same as FreeCAD addon).
// Commands: {"cmd": "...", "args": {...}}
// Responses: {"ok": true/false, "result": ..., "error": "..."}

#pragma once

#include <nlohmann/json.hpp>
#include <string>
#include <sstream>

using json = nlohmann::json;

namespace chrono_daemon {

/// Read a single newline-delimited JSON message from a stream buffer.
/// Returns false if the connection is closed.
inline bool read_message(int sockfd, std::string& buffer, json& out_msg) {
    char buf[4096];
    while (buffer.find('\n') == std::string::npos) {
        ssize_t n = recv(sockfd, buf, sizeof(buf), 0);
        if (n <= 0) return false;
        buffer.append(buf, n);
    }
    auto pos = buffer.find('\n');
    std::string line = buffer.substr(0, pos);
    buffer = buffer.substr(pos + 1);
    out_msg = json::parse(line);
    return true;
}

/// Send a JSON response as a newline-delimited message.
inline bool send_response(int sockfd, const json& resp) {
    std::string msg = resp.dump() + "\n";
    const char* data = msg.c_str();
    size_t remaining = msg.size();
    while (remaining > 0) {
        ssize_t n = send(sockfd, data, remaining, 0);
        if (n <= 0) return false;
        data += n;
        remaining -= n;
    }
    return true;
}

/// Build an OK response.
inline json ok_response(const json& result) {
    return {{"ok", true}, {"result", result}};
}

/// Build an error response.
inline json error_response(const std::string& message) {
    return {{"ok", false}, {"error", message}};
}

} // namespace chrono_daemon
