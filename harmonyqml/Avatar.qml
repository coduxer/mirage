import QtQuick 2.7
import QtQuick.Controls 2.0
import QtQuick.Layouts 1.4

Item {
    property bool invisible: false
    property string username: "?"
    property var imageSource: null
    property int dimmension: 48

    id: root
    width: dimmension
    height: invisible ? 1 : dimmension

    Rectangle {
        id: letterRectangle
        anchors.fill: parent
        visible: ! invisible && imageSource === null
        color: Qt.hsla(Backend.hueFromString(username), 0.22, 0.5, 1)

        PlainLabel {
            anchors.centerIn: parent
            text: username.charAt(0)
            color: "white"
            font.pixelSize: letterRectangle.height / 1.4
        }
    }

    Image {
        id: avatarImage
        anchors.fill: parent
        visible: ! invisible && imageSource !== null

        Component.onCompleted: if (imageSource) {source = imageSource}
        asynchronous: true
        mipmap: true
        fillMode: Image.PreserveAspectCrop
        sourceSize.width: root.dimmension
    }
}
