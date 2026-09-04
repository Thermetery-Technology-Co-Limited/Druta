How to use Druta:

Druta is from Sanskrit *druta* meaning fast. In Hindu performing art, it can also denote a “rapid shift (of expressions)", which is perfect for what this project is. 

# I. Hotkeys built for gamers:

- V/F curve editing is controlled by WASD: AD changes the point, WS changes the frequency.
- Shift + one of WASD moves the points 3x faster in any given direction. 
- Ctrl+Z undoes any given changes on the curve for a generous 64 changes deep. Ctrl+Y redoes that change. 
- Ctrl+H *holds* a given point on the V/F curve.


# II. A couple useful functions:

## 1. `De-flatten`: 
When two or more points on the VF curve land on the same frequency, only the one with the lowest voltage will ever be used. For example, if 1081, 1087, and 1093mv all correspond to 2000mhz, the card will always run at 1081mv, 2000mhz. Deflatten makes sure that every point on the the V/F curve between 1000mv to 1091mv (adjustable) are *mathematically strictly increasing*. That way, you can run 1091mv immediately without a hard voltage mod. 

(Joined overclocking during the time of 4000/5000 series? It might be helpful to know that for 1000-3000 series, almost desktop every GPU can be overvolted to 1091mv by manipulating of the voltage curve. You DO NOT need to bin for voltage.)

## 2. `Hard deflattern`
**Mandatory hard mod required**
This mode is actually the opposite of deflatten. It flattens everything after 800mv (adjustable), overclocks the 800mv point to the standard P0 frequency, and flattens every point above that. That way, the driver will lock 800mv. This looks counterintuitive because it works in conjunction with a hardware voltage mod and a completely inoperable `refin_adj` (or whatever that the BIOS uses to control the voltage internally) to neutralize *imperfect power limit bypasses*. 

*Imperfect power limit bypasses* are for GPUs with no XOC BIOS and don't completely work with shunt mods, such as Titan Xp, 2x8-pin 3080, 4080 Super, 3060, etc. These GPUs still power throttle after shunt mods, even at low percentage of TDP. The power estimation comes from the core and cannot be easily bypassed. By fooling the GPU core that it's at 800mv, you lower the internal power limit reading. However, because you took out `refin_adj` or similar, the core is actually at whatever higher voltage that you set it at with your external hard mod, which is why I made you check a box to make sure that you have both setup in place. 

## 3. (my favorite function) `Max it`: 

Had enough with boring sliders to the maximum? Click "max it". It does the V/F deflatten, maxes out the voltage boost, power limit, fan, and holds at 1093mv all in one click. You click it once, and the rest is the actual part of overclocking: changing the frequency. 

## 4. What about XBAR? 

On 10, I have found no manipulable software knobs to tune XBAR. To help you cope and seethe, you can read the the 1000 series XBAR frequence inside the monitor tab of Druta but not change it, because NVIDIA simply doesn't expose any way to change it. The good news:

### For 10 and 20 series, XBAR strictly scales with CORE FREQUENCY, NOT NVVDD VOTLAGE. 

That means you should just overclock the crap out of your core like everyone else instead of worrying about NVVDD. 

## 5. Shunt mod corrected power

It currently lives under taskbar > Device > `Shunt mod corrected power`. Simply type in the new effective resistance value to correct the power reading. Planned in the next release is a better per rail calibration. 

# III. How to load `nvtune`?

`nvtune` is shipped by Seby. You must enable test signing for it to work on your machine. Druta can hunt for it on your desktop and will load it automatically. Druta is an offline tool. It does not download or upload anything.  

You should almost always use `Read memory timings (will hold P0)` (blue) because changing P states can change timings, and reading/changing memory timing when the card is idling at P16 is useless for your endeavors. `read timing` is for sanity checks after you have applied your changes. 

`Load nvtune` and `Enable Test Signing` are conspicuously displayed when nvtune isn't loaded.  

Once `nvtune` EXE is loaded, these buttons move up to the `Device` menus on the taskbar. 
