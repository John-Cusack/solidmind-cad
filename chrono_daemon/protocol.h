// protocol.h — JSON protocol handling for the Chrono daemon.
//
// Uses nlohmann/json for parsing/serialization.
// Protocol: newline-delimited JSON, Engine Integration Contract v1
// (docs/engine-contract.md).
// Commands:  {"cmd": "...", "args": {...}, "request_id": "..."}
// Responses: {"ok": true,  "result": ..., "request_id": "..."}
//            {"ok": false, "error": {"code": "...", "message": "..."},
//             "request_id": "..."}
// ``request_id`` is opaque: echoed verbatim when present, omitted when absent.

#pragma once

#include <nlohmann/json.hpp>
#include <string>
#include <sstream>

using json = nlohmann::json;

namespace chrono_daemon {

/// Contract v1 wire literals.  Duplicated per bridge on purpose — the
/// contract is data, not a shared library (architecture doc, Principle 2).
inline constexpr const char* PROTOCOL_VERSION = "1.0.0";
inline constexpr const char* CONTRACT_VERSION = "1";
inline constexpr const char* DAEMON_VERSION = "1.0.0";

/// Read a single newline-delimited JSON message from a stream buffer.
/// Returns false if the connection is closed.  ``out_ok`` is false when the
/// line arrived but was not parseable JSON — the caller answers INVALID_JSON
/// rather than dropping the connection.
inline bool read_message(int sockfd, std::string& buffer, json& out_msg, bool& out_ok) {
    char buf[4096];
    while (buffer.find('\n') == std::string::npos) {
        ssize_t n = recv(sockfd, buf, sizeof(buf), 0);
        if (n <= 0) return false;
        buffer.append(buf, n);
    }
    auto pos = buffer.find('\n');
    std::string line = buffer.substr(0, pos);
    buffer = buffer.substr(pos + 1);
    out_ok = true;
    try {
        out_msg = json::parse(line);
    } catch (const json::parse_error&) {
        out_msg = json::object();
        out_ok = false;
    }
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

/// Build an error response with a contract error code (§4).
inline json error_response(const std::string& code, const std::string& message) {
    return {{"ok", false}, {"error", {{"code", code}, {"message", message}}}};
}

/// Echo the request's opaque ``request_id`` onto a response when the request
/// carried one; leave the response untouched otherwise (contract §1).
inline void echo_request_id(json& response, const json& msg) {
    if (msg.is_object()) {
        auto it = msg.find("request_id");
        if (it != msg.end() && it->is_string()) {
            response["request_id"] = *it;
        }
    }
}

} // namespace chrono_daemon
