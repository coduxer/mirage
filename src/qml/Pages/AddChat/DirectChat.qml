import QtQuick 2.12
import QtQuick.Layouts 1.12
import "../../Base"

HBox {
    id: addChatBox
    clickButtonOnEnter: "apply"

    onFocusChanged: userField.forceActiveFocus()

    buttonModel: [
        {
            name: "apply",
            text: qsTr("Start chat"),
            iconName: "start-direct-chat",
            enabled: Boolean(userField.text.trim())
        },
        { name: "cancel", text: qsTr("Cancel"), iconName: "cancel" },
    ]

    buttonCallbacks: ({
        apply: button => {
            button.loading    = true
            errorMessage.text = ""

            let args = [userField.text.trim(), encryptCheckBox.checked]

            py.callClientCoro(userId, "new_direct_chat", args, roomId => {
                button.loading    = false
                errorMessage.text = ""
                pageLoader.showRoom(userId, roomId)

            }, (type, args) => {
                button.loading = false

                let txt = qsTr("Unknown error - %1: %2").arg(type).arg(args)

                if (type === "InvalidUserInContext")
                    txt = qsTr("Can't start chatting with yourself")

                if (type === "InvalidUserId")
                    txt = qsTr("Invalid user ID, expected format is " +
                               "@username:homeserver")

                if (type === "UserNotFound")
                    txt = qsTr("This user does not exist")

                errorMessage.text = txt
            })
        },

        cancel: button => {
            userField.text    = ""
            errorMessage.text = ""
            pageLoader.showPrevious()
        }
    })


    readonly property string userId: addChatPage.userId


    CurrentUserAvatar {
        Layout.alignment: Qt.AlignCenter
        Layout.preferredWidth: 128
        Layout.preferredHeight: Layout.preferredWidth
    }

    HTextField {
        id: userField
        placeholderText: qsTr("Peer user ID (e.g. @bob:matrix.org)")
        error: Boolean(errorMessage.text)

        Layout.fillWidth: true
    }

    EncryptCheckBox {
        id: encryptCheckBox

        Layout.fillWidth: true
    }

    HLabel {
        id: errorMessage
        wrapMode: Text.Wrap
        horizontalAlignment: Text.AlignHCenter
        color: theme.colors.errorText

        visible: Layout.maximumHeight > 0
        Layout.maximumHeight: text ? implicitHeight : 0
        Behavior on Layout.maximumHeight { HNumberAnimation {} }

        Layout.fillWidth: true
    }
}
