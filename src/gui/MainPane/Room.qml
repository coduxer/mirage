// SPDX-License-Identifier: LGPL-3.0-or-later

import QtQuick 2.12
import QtQuick.Layouts 1.12
import Clipboard 0.1
import ".."
import "../Base"

HTileDelegate {
    spacing: theme.spacing
    backgroundColor: theme.mainPane.room.background
    opacity: model.left ? theme.mainPane.room.leftRoomOpacity : 1

    image: HRoomAvatar {
        roomId: model.id
        displayName: model.display_name
        mxc: model.avatar_url
    }

    title.color: theme.mainPane.room.name
    title.text: model.display_name || qsTr("Empty room")

    additionalInfo.children: HIcon {
        svgName: "invite-received"
        colorize: theme.colors.alertBackground

        Layout.maximumWidth: invited ? implicitWidth : 0

        Behavior on Layout.maximumWidth { HNumberAnimation {} }
    }

    subtitle.color: theme.mainPane.room.subtitle
    subtitle.textFormat: Text.StyledText
    subtitle.font.italic:
        lastEvent && lastEvent.event_type === "RoomMessageEmote"
    subtitle.text: {
        if (! lastEvent) return ""

        const isEmote      = lastEvent.event_type === "RoomMessageEmote"
        const isMsg        = lastEvent.event_type.startsWith("RoomMessage")
        const isUnknownMsg = lastEvent.event_type === "RoomMessageUnknown"
        const isCryptMedia = lastEvent.event_type.startsWith("RoomEncrypted")

        // If it's a general event
        if (isEmote || isUnknownMsg || (! isMsg && ! isCryptMedia))
            return utils.processedEventText(lastEvent)

        const text = utils.coloredNameHtml(
            lastEvent.sender_name, lastEvent.sender_id
        ) + ": " + lastEvent.inline_content

        return text.replace(
            /< *span +class=['"]?quote['"]? *>(.+?)<\/ *span *>/g,
            `<font color="${theme.mainPane.room.subtitleQuote}">$1</font>`,
        )
    }

    rightInfo.color: theme.mainPane.room.lastEventDate
    rightInfo.text: {
        model.last_event_date < new Date(1) ?
        "" :

        utils.dateIsToday(model.last_event_date) ?
        utils.formatTime(model.last_event_date, false) :  // no seconds

        model.last_event_date.getFullYear() === new Date().getFullYear() ?
        Qt.formatDate(model.last_event_date, "d MMM") :  // e.g. "5 Dec"

        // model.last_event_date.getFullYear() ?
        Qt.formatDate(model.last_event_date, "MMM yyyy")  // e.g. "Jan 2020"
    }

    contextMenu: HMenu {
        HMenuItemPopupSpawner {
            visible: joined
            enabled: model.can_invite
            icon.name: "room-send-invite"
            text: qsTr("Invite members")

            popup: "Popups/InviteToRoomPopup.qml"
            properties: ({
                userId: userId,
                roomId: model.id,
                roomName: model.display_name,
                invitingAllowed: Qt.binding(() => model.can_invite)
            })
        }

        HMenuItem {
            icon.name: "copy-room-id"
            text: qsTr("Copy room ID")
            onTriggered: Clipboard.text = model.id
        }

        HMenuItem {
            visible: invited
            icon.name: "invite-accept"
            icon.color: theme.colors.positiveBackground
            text: qsTr("Accept %1's invite").arg(utils.coloredNameHtml(
                model.inviter_name, model.inviter_id
            ))
            label.textFormat: Text.StyledText

            onTriggered: py.callClientCoro(
                userId, "join", [model.id]
            )
        }

        HMenuItemPopupSpawner {
            visible: invited || joined
            icon.name: invited ? "invite-decline" : "room-leave"
            icon.color: theme.colors.negativeBackground
            text: invited ? qsTr("Decline invite") : qsTr("Leave")

            popup: "Popups/LeaveRoomPopup.qml"
            properties: ({
                userId: userId,
                roomId: model.id,
                roomName: model.display_name,
            })
        }

        HMenuItemPopupSpawner {
            icon.name: "room-forget"
            icon.color: theme.colors.negativeBackground
            text: qsTr("Forget")

            popup: "Popups/ForgetRoomPopup.qml"
            autoDestruct: false
            properties: ({
                userId: userId,
                roomId: model.id,
                roomName: model.display_name,
            })
        }
    }

    onActivated: {
        pageLoader.showRoom(userId, model.id)
        mainPaneList.detachedCurrentIndex = false
    }


    property string userId
    readonly property bool joined: ! invited && ! parted
    readonly property bool invited: model.inviter_id && ! parted
    readonly property bool parted: model.left

    readonly property ListModel eventModel:
        ModelStore.get(userId, model.id, "events")

    readonly property QtObject lastEvent:
        eventModel.count > 0 ? eventModel.get(0) : null


    Behavior on opacity { HNumberAnimation {} }
}