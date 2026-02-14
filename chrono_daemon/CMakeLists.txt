cmake_minimum_required(VERSION 3.16)
project(chrono_daemon CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# ─── Find Project Chrono ───
# Pass -DChrono_DIR=/path/to/chrono/build when configuring, e.g.:
#   cmake .. -DChrono_DIR=$HOME/chrono/build
find_package(Chrono REQUIRED)

# ─── Find nlohmann/json ───
# Prefer system package; falls back to FetchContent if not found.
find_package(nlohmann_json QUIET)
if(NOT nlohmann_json_FOUND)
    include(FetchContent)
    FetchContent_Declare(
        nlohmann_json
        GIT_REPOSITORY https://github.com/nlohmann/json.git
        GIT_TAG        v3.11.3
        GIT_SHALLOW    TRUE
    )
    FetchContent_MakeAvailable(nlohmann_json)
endif()

# ─── Chrono daemon executable ───
add_executable(chrono_daemon
    main.cpp
)

target_include_directories(chrono_daemon PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}
    ${CHRONO_INCLUDE_DIRS}
)

target_link_libraries(chrono_daemon PRIVATE
    ${CHRONO_LIBRARIES}
    nlohmann_json::nlohmann_json
)

target_compile_definitions(chrono_daemon PRIVATE
    ${CHRONO_CXX_FLAGS}
)

# ─── Install ───
install(TARGETS chrono_daemon RUNTIME DESTINATION bin)
