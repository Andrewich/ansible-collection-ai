# Reduce a Redfish payload to the identity lines, keeping the path of each value.
#
# The payloads of some vendors are not valid JSON (Huawei iBMC, for example, mixes an
# object and an array under "Processors"), so this works line by line on the pretty
# printed document and tracks nesting by indentation instead of parsing.
#
# Kept:
#   * top-level Manufacturer / Model  (the identity of the whole server)
#   * Model values under a Processors path (the actual CPU strings)
#   * shallow ProductName (an OEM alias of the system model)
# Everything else, including mainboard and CPU manufacturer fields, is dropped so the
# 350M model does not have to tell "MiTAC" from "FUJITSU" by itself.
#
# Usage: awk -f redfish_paths.awk payload.json | awk '!seen[$0]++'

{
    ind = match($0, /[^ ]/)
    if (ind == 0) next
    ind = ind - 1
    rest = substr($0, ind + 1)
    if (substr(rest, 1, 1) != "\"") next

    close_q = index(substr(rest, 2), "\"")
    if (close_q == 0) next
    key = substr(rest, 2, close_q - 1)
    after = substr(rest, close_q + 2)
    if (after !~ /^[ \t]*:/) next

    # maintain the path stack: a new key closes every deeper or same-level one
    while (top > 0 && sind[top] >= ind) top--
    top++
    skey[top] = key
    sind[top] = ind

    path = ""
    for (i = 1; i <= top; i++) path = (path == "" ? skey[i] : path "." skey[i])

    system_level = (key == "Manufacturer" || key == "Model") && ind <= 2
    cpu = (key == "Model") && (path ~ /Processors/)
    product = (key == "ProductName") && ind <= 4
    if (!(system_level || cpu || product)) next

    val = after
    sub(/^[ \t]*:[ \t]*/, "", val)
    sub(/,[ \t]*$/, "", val)
    gsub(/^"|"$/, "", val)
    print path " = " val
}
