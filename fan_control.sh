#!/bin/bash

# User-defined temperature and PWM variables
CPU_LOW_TEMP=40        # Low temperature threshold for CPU
CPU_HIGH_TEMP=80       # High temperature threshold for CPU
DRIVE_LOW_TEMP=25      # Low temperature threshold for Drives
DRIVE_HIGH_TEMP=45     # Low temperature threshold for Drives
NVME_LOW_TEMP=35       # Low temperature threshold for NVMe
NVME_HIGH_TEMP=60      # High temperature threshold for NVMe
MIN_PWM=70             # Minimum PWM value
MAX_PWM=255            # Maximum PWM value
OVERHEAT_THRESHOLD=10  # Temperature threshold above high temp for overheating protection

# Command line controlled variables
SLEEP_DURATION=5       # Default sleep duration between checks
OVERHEAT_PROTECTION=false  # Enable or disable overheating protection
VERBOSE=false          # Verbose flag
PWM_METHOD="lin"       # Default PWM calculation method

# Function to print messages based on verbose flag
log() {
    if [ "$VERBOSE" = true ]; then
        echo "$1"
    fi
}

# Function to log errors
log_error() {
    logger "$1"
    echo "$1" >&2
}

# Function to shut down the system
shutdown_system() {
    log_error "Overheating detected. Shutting down the system immediately."
    shutdown -h now
}

# Function to calculate interpolated PWM value
calculate_pwm() {
    local temp=$1
    local low_temp=$2
    local high_temp=$3

    if [ "$temp" -lt "$low_temp" ]; then
        echo 0  # Turn off the fan if the temperature is below the low threshold
        return
    elif [ "$temp" -ge "$high_temp" ]; then
        echo $MAX_PWM
        return
    fi

    case $PWM_METHOD in
        lin)
            local range_temp=$((high_temp - low_temp))
            local range_pwm=$((MAX_PWM - MIN_PWM))
            local temp_offset=$((temp - low_temp))
            local pwm_value=$((MIN_PWM + (temp_offset * range_pwm / range_temp)))
            echo $pwm_value
            ;;
        log)
            local range_temp=$((high_temp - low_temp))
            local temp_offset=$((temp - low_temp + 1))
            local pwm_value=$(awk -v min=$MIN_PWM -v max=$MAX_PWM -v temp=$temp_offset -v range=$range_temp 'BEGIN { print min + (max - min) * log(temp) / log(range + 1) }')
            echo ${pwm_value%.*}
            ;;
        exp)
            local range_temp=$((high_temp - low_temp))
            local temp_offset=$((temp - low_temp))
            local pwm_value=$(awk -v min=$MIN_PWM -v max=$MAX_PWM -v temp=$temp_offset -v range=$range_temp 'BEGIN { print min + (max - min) * ((exp(temp / range) - 1) / (exp(1) - 1)) }')
            echo ${pwm_value%.*}
            ;;
        *)
            log_error "Unknown PWM calculation method: $PWM_METHOD"
            exit 1
            ;;
    esac
}

# Function to check if the it87 module is loaded, and load it if necessary
check_and_load_module() {
    if ! lsmod | grep -q it87; then
        log "it87 module is not loaded. Attempting to load it..."
        if sudo modprobe -a it87; then
            log "it87 module loaded successfully."
        else
            log_error "Failed to load it87 module. Exiting."
            exit 1
        fi
    else
        log "it87 module is already loaded."
    fi
}

# Parse command line options
while getopts "vt:oc:" opt; do
    case ${opt} in
        v )
            VERBOSE=true
            ;;
        t )
            SLEEP_DURATION=$OPTARG
            ;;
        o )
            OVERHEAT_PROTECTION=true
            ;;
        c )
            PWM_METHOD=$OPTARG
            ;;
        \? )
            echo "Usage: $0 [-v] [-t sleep_duration] [-o] [-c pwm_method]"
            echo "Options:"
            echo "  -v                  Enable verbose mode."
            echo "  -t sleep_duration   Set the sleep duration between checks (default: 5 seconds)."
            echo "  -o                  Enable overheating protection."
            echo "  -c pwm_method       Set the fan curve to linear (lin), logarithmic (log), or exponential (exp). Default is linear (lin)."
            exit 1
            ;;
    esac
done

# Print the current user, sleep duration, overheating protection status, and PWM method
log "Current user: $(whoami)"
log "Sleep duration: ${SLEEP_DURATION} seconds"
log "Overheating protection enabled: $OVERHEAT_PROTECTION"
log "PWM calculation method: $PWM_METHOD"

# Check if the script is run as root, otherwise re-run with sudo
[ "$EUID" -eq 0 ] || exec sudo "$0" "$@"

# Check and load the it87 module if necessary
check_and_load_module

# Get the fan speed and fan RPM directories
fanSpeedDir=$(echo /sys/class/hwmon/$(ls -l /sys/class/hwmon | grep it87 | awk '{print $9}')/pwm2)
fanRpmDir=$(echo /sys/class/hwmon/$(ls -l /sys/class/hwmon | grep it87 | awk '{print $9}')/fan2_input)
# Get the CPU temperature directory
cpuTempDir=$(echo /sys/class/hwmon/$(ls -l /sys/class/hwmon | grep coretemp | awk '{print $9}')/temp1_input)

# Function to dynamically find hwmon entries for drivetemp and nvme
get_hwmon_dirs() {
    local type=$1
    local dirs=()
    for dir in /sys/class/hwmon/*; do
        if grep -q "$type" "$dir/name"; then
            dirs+=("$dir")
        fi
    done
    echo "${dirs[@]}"
}

# Get drivetemp and nvme directories
driveTempDirs=($(get_hwmon_dirs "drivetemp"))
nvmeTempDirs=($(get_hwmon_dirs "nvme"))

# Check if fan speed, fan RPM, and CPU temperature directories exist
if [ ! -e "$fanSpeedDir" ] || [ ! -e "$fanRpmDir" ] || [ ! -e "$cpuTempDir" ]; then
    log_error "Error: fan speed, fan RPM, or CPU temperature directory not found."
    exit 1
fi

# Initialise cooldown.
cooldown=0

# Infinite loop to monitor and control fan speed based on the highest temperature
while true; do
    # Read the current fan speed, fan RPM, and CPU temperature
    fanSpeed=$(<"$fanSpeedDir")
    fanRpm=$(<"$fanRpmDir")
    cpuTemp=$(<"$cpuTempDir")
    cpuTemp=${cpuTemp:0:2}  # Extract the first two digits of the temperature

    # Check for broken fan
    if [ "$fanSpeed" -ne 0 ] && [ "$fanRpm" -eq 0 ]; then
        log_error "Error: Fan is broken. PWM is $fanSpeed but RPM is 0."
        exit 1
    fi

    # Overheating protection check for CPU
    if [ "$OVERHEAT_PROTECTION" = true ] && [ "$cpuTemp" -ge $((CPU_HIGH_TEMP + OVERHEAT_THRESHOLD)) ]; then
        log_error "Error: CPU temperature ($cpuTemp°C) exceeded the overheating threshold."
        shutdown_system
    fi

    # Initialize maxTemp with the CPU temperature
    maxCPU=$cpuTemp

    # Check drive temperatures
    maxDrive=0
    for dir in "${driveTempDirs[@]}"; do
        driveTemp=$(<"$dir/temp1_input")
        driveTemp=${driveTemp:0:2}  # Extract the first two digits of the temperature
        if [ "$driveTemp" -gt "$maxDrive" ]; then
            maxDrive=$driveTemp
        fi

        # Overheating protection check for Drives
        if [ "$OVERHEAT_PROTECTION" = true ] && [ "$driveTemp" -ge $((DRIVE_HIGH_TEMP + OVERHEAT_THRESHOLD)) ]; then
            log_error "Error: Drive temperature ($driveTemp°C) exceeded the overheating threshold."
            shutdown_system
        fi
    done

    # Check NVMe temperatures
    maxNVMe=0
    for dir in "${nvmeTempDirs[@]}"; do
        nvmeTemp=$(<"$dir/temp1_input")
        nvmeTemp=${nvmeTemp:0:2}  # Extract the first two digits of the temperature
        if [ "$nvmeTemp" -gt "$maxNVMe" ]; then
            maxNVMe=$nvmeTemp
        fi

        # Overheating protection check for NVMe
        if [ "$OVERHEAT_PROTECTION" = true ] && [ "$nvmeTemp" -ge $((NVME_HIGH_TEMP + OVERHEAT_THRESHOLD)) ]; then
            log_error "Error: NVMe temperature ($nvmeTemp°C) exceeded the overheating threshold."
            shutdown_system
        fi
    done

    # Print the highest temperatures and current fan speed/RPM
    log "Max CPU Temp(Celsius): $maxCPU"
    log "Max Drive Temp(Celsius): $maxDrive"
    log "Max NVMe Temp(Celsius): $maxNVMe"
    log "fanSpeed PWM value: $fanSpeed"
    log "fan RPM: $fanRpm"

    # Calculate PWM values for CPU, Drive, and NVMe temperatures
    pwmCPU=$(calculate_pwm "$maxCPU" "$CPU_LOW_TEMP" "$CPU_HIGH_TEMP")
    pwmDrive=$(calculate_pwm "$maxDrive" "$DRIVE_LOW_TEMP" "$DRIVE_HIGH_TEMP")
    pwmNVMe=$(calculate_pwm "$maxNVMe" "$NVME_LOW_TEMP" "$NVME_HIGH_TEMP")

    # Determine the highest PWM value
    if (( "$pwmCPU" + "$pwmDrive" + "$pwmNVMe" == 0 )); then
        if (( "$cooldown" > 0 )); then
            pwmValue=$MIN_PWM
            log "Setting PWM to MIN because of cooldown ($cooldown interval(s) left)"
            cooldown=$((cooldown - 1))
        else
            pwmValue=0
            log "No MIN thresholds met and no cooldown, fan off."
        fi
    elif (( "$pwmCPU" > "$pwmDrive" )) && (( "$pwmCPU" > "$pwmNVMe" )); then
        pwmValue=$pwmCPU
        log "Setting PWM based on CPU temperature"
        cooldown=$COOLDOWN_MAX
    elif (( "$pwmDrive" > "$pwmCPU" )) && (( "$pwmDrive" > "$pwmNVMe" )); then
        pwmValue=$pwmDrive
        log "Setting PWM based on Drive temperature"
        cooldown=$COOLDOWN_MAX
    elif (( "$pwmNVMe" > "$pwmDrive" )) && (( "$pwmNVMe" > "$pwmCPU" )); then
        pwmValue=$pwmNVMe
        log "Setting PWM based on NVMe temperature"
        cooldown=$COOLDOWN_MAX
    else
        pwmValue=$MAX_PWM
        log_error "Unknown state, turning fan on. Check your configuration."
    fi

    # Set the fan speed to the highest PWM value
    echo "$pwmValue" > "$fanSpeedDir"

    # Wait for the defined sleep duration before checking again
    sleep "$SLEEP_DURATION"
    log "============================================="
done
