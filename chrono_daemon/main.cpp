// main.cpp — Chrono daemon: TCP server for multibody dynamics simulation.
//
// Architecture mirrors the FreeCAD addon socket server:
//   - Listens on localhost:9877 (next port after FreeCAD's 9876)
//   - Accepts newline-delimited JSON commands
//   - Builds Chrono systems from mechanism definitions
//   - Runs time-domain simulations
//   - Returns results as JSON
//
// Build:
//   cd chrono_daemon && mkdir build && cd build
//   cmake .. -DChrono_DIR=/path/to/chrono/build
//   make
//
// Run:
//   ./chrono_daemon [--port 9877] [--host 127.0.0.1]

#include <iostream>
#include <string>
#include <cstring>
#include <csignal>
#include <atomic>

#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <unistd.h>
#include <getopt.h>

#include <nlohmann/json.hpp>
#include "protocol.h"
#include "mechanism_builder.h"
#include "simulator.h"

using json = nlohmann::json;
using namespace chrono_daemon;

static std::atomic<bool> g_running{true};

static void signal_handler(int) {
    g_running = false;
}

/// Handle a single JSON command and return a response.
static json handle_command(const json& msg) {
    std::string cmd;
    try {
        cmd = msg.at("cmd").get<std::string>();
    } catch (...) {
        return error_response("Missing 'cmd' field");
    }

    json args = msg.value("args", json::object());

    if (cmd == "ping") {
        return ok_response({{"pong", true}});
    }

    if (cmd == "simulate") {
        try {
            if (!args.contains("mechanism") && !args.contains("simulation_spec")) {
                return error_response("Missing 'mechanism' or 'simulation_spec' in args");
            }

            double duration_s = args.value("duration_s", 1.0);
            double dt_s = args.value("dt_s", 0.001);
            double output_interval = args.value("output_interval", 0.01);

            // Build Chrono system — new spec path or legacy mechanism path
            auto built = args.contains("simulation_spec")
                ? build_mechanism_from_spec(args["simulation_spec"])
                : build_mechanism(args["mechanism"]);

            if (!built.warnings.empty()) {
                // Log warnings but continue
                std::cerr << "[chrono_daemon] Build warnings:" << std::endl;
                for (const auto& w : built.warnings) {
                    std::cerr << "  - " << w << std::endl;
                }
            }

            // Run simulation
            json result = run_simulation(built, duration_s, dt_s, output_interval);

            // Include build warnings in result
            if (!built.warnings.empty()) {
                json warnings_json = json::array();
                for (const auto& w : built.warnings) {
                    warnings_json.push_back(w);
                }
                result["warnings"] = warnings_json;
            }

            return ok_response(result);

        } catch (const std::exception& e) {
            return error_response(std::string("Simulation error: ") + e.what());
        }
    }

    if (cmd == "shutdown") {
        g_running = false;
        return ok_response({{"message", "Shutting down"}});
    }

    return error_response("Unknown command: " + cmd);
}

/// Handle a single client connection (blocking).
static void handle_client(int client_fd) {
    std::string buffer;
    json msg;

    std::cerr << "[chrono_daemon] Client connected" << std::endl;

    while (g_running) {
        if (!read_message(client_fd, buffer, msg)) {
            break;  // Client disconnected
        }

        json response = handle_command(msg);

        if (!send_response(client_fd, response)) {
            break;  // Send failed
        }
    }

    close(client_fd);
    std::cerr << "[chrono_daemon] Client disconnected" << std::endl;
}

int main(int argc, char* argv[]) {
    std::string host = "127.0.0.1";
    int port = 9877;

    // Parse command-line options
    static struct option long_options[] = {
        {"host", required_argument, nullptr, 'h'},
        {"port", required_argument, nullptr, 'p'},
        {"help", no_argument, nullptr, '?'},
        {nullptr, 0, nullptr, 0}
    };

    int opt;
    while ((opt = getopt_long(argc, argv, "h:p:", long_options, nullptr)) != -1) {
        switch (opt) {
            case 'h': host = optarg; break;
            case 'p': port = std::stoi(optarg); break;
            default:
                std::cerr << "Usage: chrono_daemon [--host HOST] [--port PORT]" << std::endl;
                return 1;
        }
    }

    // Set up signal handlers
    signal(SIGINT, signal_handler);
    signal(SIGTERM, signal_handler);

    // Create TCP socket
    int server_fd = socket(AF_INET, SOCK_STREAM, 0);
    if (server_fd < 0) {
        std::cerr << "Failed to create socket" << std::endl;
        return 1;
    }

    int reuse = 1;
    setsockopt(server_fd, SOL_SOCKET, SO_REUSEADDR, &reuse, sizeof(reuse));

    struct sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(port);
    inet_pton(AF_INET, host.c_str(), &addr.sin_addr);

    if (bind(server_fd, (struct sockaddr*)&addr, sizeof(addr)) < 0) {
        std::cerr << "Failed to bind to " << host << ":" << port << std::endl;
        close(server_fd);
        return 1;
    }

    if (listen(server_fd, 1) < 0) {
        std::cerr << "Failed to listen" << std::endl;
        close(server_fd);
        return 1;
    }

    std::cerr << "[chrono_daemon] Listening on " << host << ":" << port << std::endl;

    while (g_running) {
        // Accept one client at a time (same as FreeCAD addon pattern)
        struct sockaddr_in client_addr{};
        socklen_t client_len = sizeof(client_addr);

        // Use a timeout so we can check g_running
        struct timeval tv{};
        tv.tv_sec = 1;
        tv.tv_usec = 0;
        setsockopt(server_fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));

        int client_fd = accept(server_fd, (struct sockaddr*)&client_addr, &client_len);
        if (client_fd < 0) {
            continue;  // Timeout or interrupt, check g_running
        }

        handle_client(client_fd);
    }

    close(server_fd);
    std::cerr << "[chrono_daemon] Shut down" << std::endl;
    return 0;
}
