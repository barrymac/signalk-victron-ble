# Add Engine Running State Detection via Victron Chargers

## Summary  
This enhancement enables Victron BLE-enabled chargers/converter devices to report boat engine running status via their charging activity. Charger states now map directly to numeric engine operational status suitable for integration with automation systems.

## Key Features 🔄
**1. Engine Running Status Detection**  
- **1 = Engine Running**:  
  `Bulk`, `Absorption`, `Float`, `Storage`, or `Equalize` charging modes  
- **0 = Engine Stopped**:  
  `Off`, `Disconnected`, or any error state  

**2. Plugin Configuration**  
```json
{
  "linkToEngine": true,
  "engineId": "portEngine" 
}
```
Toggle per-device engine linking in SignalK UI

**3. Error Handling**  
- Errors (overvoltage/current) automatically set `0` state  
- Warns in logs while maintaining safety state  

## Why This Matters ⚙️  
- Eliminates dedicated engine sensors when using alternator-powered chargers  
- Automates generator/shore power detection via charger activity  
- Compatible with automation relying on numeric states (0/1)

## Setup Instructions 🛠️  
1. In plugin config:  
   ✔️ Enable *"Link Charger State to Engine Status"*  
   🆔 Set *Engine ID* (e.g. "portEngine")  
2. Status appears under:  
   ```text
   propulsion.<engineId>.state.value ➔ 0|1
   ```

## Verification Steps ✅  
```logs
# Active charging → Engine started 
[DEBUG] Charge: bulk → Engine State: 1 (portEngine)

# Charger disconnected → Engine stopped  
[DEBUG] Charge: disconnected → Engine State: 0 (portEngine)
```

## Compatibility  
✅ **Devices**:  
   - Orion XS (tested)  
   - DC-DC Converters  
⛔ **Requires**:  
   - Victron BLE v0.16+ library  
   - Bleak 0.20+ BLE backend  

Resolves #12 (Engine Status Automation)

