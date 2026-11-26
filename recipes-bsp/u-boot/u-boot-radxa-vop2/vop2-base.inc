# Shared by the two opt-in -R profiles; never included by the default recipe.
FILESEXTRAPATHS:prepend:pn-u-boot-radxa:radxa-zero3w := "${YOCTO_PROJECT_ROOT}/recipes/retrofactory/meta-radxazero3w/recipes-bsp/u-boot/u-boot-radxa-vop2:"
SRC_URI:append:pn-u-boot-radxa:radxa-zero3w = " \
    file://01-upstream.patch \
    file://02-upstream.patch \
    file://03-upstream.patch \
    file://04-upstream.patch \
    file://05-upstream.patch \
    file://06-upstream.patch \
    file://07-upstream.patch \
    file://08-upstream.patch \
    file://09-initialize-output-reg.patch \
    file://arcadia-boot-artwork.patch \
    file://vop2-video.cfg \
"
