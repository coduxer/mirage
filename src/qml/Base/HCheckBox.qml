import QtQuick 2.12
import QtQuick.Controls 2.12
import QtQuick.Layouts 1.12

CheckBox {
    id: box
    checked: defaultChecked
    spacing: theme.spacing
    padding: 0

    indicator: Rectangle {
        opacity: box.enabled ? 1 : theme.disabledElementsOpacity + 0.2
        implicitWidth: theme.controls.checkBox.boxSize
        implicitHeight: implicitWidth
        x: box.leftPadding
        y: box.topPadding + box.availableHeight / 2 - height / 2
        radius: theme.radius / 1.5

        color: theme.controls.checkBox.boxBackground
        border.color:
            box.enabled && box.pressed ?
            theme.controls.checkBox.boxPressedBorder :

            (box.enabled && box.hovered) || box.activeFocus ?
            theme.controls.checkBox.boxHoveredBorder :

            theme.controls.checkBox.boxBorder

        Behavior on border.color { HColorAnimation { factor: 0.5 } }

        HIcon {
            anchors.centerIn: parent
            dimension: parent.width - 2
            svgName: "check-mark"
            colorize: theme.controls.checkBox.checkIconColorize

            scale: box.checked ? 1 : 0

            Behavior on scale {
                HNumberAnimation {
                    overshoot: 4
                    easing.type: Easing.InOutBack
                }
            }
        }
    }

    contentItem: HColumnLayout {
        opacity: box.enabled ? 1 : theme.disabledElementsOpacity

        HLabel {
            id: mainText
            text: box.text
            color: theme.controls.checkBox.text

            // Set a width on CheckBox for wrapping to work,
            // e.g. by using Layout.fillWidth
            wrapMode: Text.Wrap
            leftPadding: box.indicator.width + box.spacing
            verticalAlignment: Text.AlignVCenter

            Layout.fillWidth: true
        }

        HLabel {
            id: subtitleText
            visible: Boolean(text)
            color: theme.controls.checkBox.subtitle
            font.pixelSize: theme.fontSize.small

            wrapMode: mainText.wrapMode
            leftPadding: mainText.leftPadding
            verticalAlignment: mainText.verticalAlignment

            Layout.fillWidth: true
        }
    }


    property alias mainText: mainText
    property alias subtitle: subtitleText
    property bool defaultChecked: false
    readonly property bool changed: checked !== defaultChecked


    function reset() { checked = defaultChecked }


    Behavior on opacity { HNumberAnimation { factor: 2 } }
}
