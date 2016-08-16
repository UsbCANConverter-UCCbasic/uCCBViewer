/*
 * USBtinViewer - Simple GUI for USBtin - USB to CAN interface
 * http://www.fischl.de/usbtin
 *
 * Notes:
 * - The timestamp is generated in the application on the host, the hardware
 *   timestamping is currently not used!
 * - Disable "Follow" on high-loaded busses!
 *
 * Copyright (C) 2014-2016  Thomas Fischl 
 * 
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 * 
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 * 
 * You should have received a copy of the GNU General Public License
 * along with this program.  If not, see <http://www.gnu.org/licenses/>.
 */

import uCCBlib.*;

import javax.swing.*;
import javax.swing.event.DocumentEvent;
import javax.swing.event.DocumentListener;
import javax.swing.table.DefaultTableCellRenderer;
import javax.swing.table.TableColumnModel;
import jssc.SerialPortList;

import java.awt.datatransfer.StringSelection;
import java.awt.event.ActionEvent;
import java.awt.event.MouseAdapter;
import java.awt.event.MouseEvent;

import static java.awt.Toolkit.getDefaultToolkit;
import static java.lang.System.getProperty;
import java.util.logging.Level;
import java.util.logging.Logger;

/**
 * Main window frame for USBtinViewer
 * 
 * @author Thomas Fischl
 */
public class USBtinViewer extends javax.swing.JFrame implements CANMessageListener {

    /** Version string */
    protected final String version = "0.1";

    /** USBtin device */
    protected USBtin usbtin = new USBtin();
    
    /** Input fields containing payload data */    
    protected JTextField[] msgDataFields;
    
    /** True, if converting between message string <-> input fields in process */
    protected boolean disableMsgUpdate = false;
    
    /** Start timestamp in system-milliseconds */
    protected long baseTimestamp = 0;

    /**
     * Creates new form and initialize it
     */
    public USBtinViewer() {
        
        // init view components
        initComponents();
        
        CreateFiltersArray(this.jPanel2,2);
        
        setTitle(getTitle() + " " + version);
        setIconImage(new ImageIcon(getClass().getResource("/res/icons/usbtinviewer.png")).getImage());
        openmodeComboBox.setSelectedItem(USBtin.OpenMode.ACTIVE);
        
        monitorTable.setModel(new MonitorMessageTableModel());
        // initialize message payload input fields and add listeners
        msgDataFields = new JTextField[]{msgData0, msgData1, msgData2, msgData3, msgData4, msgData5, msgData6, msgData7};
        DocumentListener msgListener = new DocumentListener() {
            @Override
            public void insertUpdate(DocumentEvent de) {
                msgFields2msgString();
            }

            @Override
            public void removeUpdate(DocumentEvent de) {
                msgFields2msgString();
            }

            @Override
            public void changedUpdate(DocumentEvent de) {
                msgFields2msgString();
            }
        };
        msgId.getDocument().addDocumentListener(msgListener);
        for (JTextField msgDataField : msgDataFields) {
            msgDataField.getDocument().addDocumentListener(msgListener);
        }

        // add listener to message string input field
        sendMessage.getDocument().addDocumentListener(new DocumentListener() {

            @Override
            public void insertUpdate(DocumentEvent de) {
                msgString2msgFields();
            }

            @Override
            public void removeUpdate(DocumentEvent de) {
                msgString2msgFields();
            }

            @Override
            public void changedUpdate(DocumentEvent de) {
                msgString2msgFields();
            }
        });

        logTable.addMouseListener(new MouseAdapter() {
            private boolean isIoType(LogMessage message) {
                LogMessage.MessageType type = message.getType();
                return (type == LogMessage.MessageType.OUT ||
                        type == LogMessage.MessageType.IN);
            }

            // "Popup menus are triggered differently on different systems.
            // Therefore, isPopupTrigger should be checked in both
            // mousePressed and mouseReleased"
            @Override
            public void mousePressed(MouseEvent e) {
                if (e.isPopupTrigger()) {
                    mouseReleased(e);
                }
            }

            @Override
            public void mouseReleased(MouseEvent e) {
                if (e.isPopupTrigger()) {
                    final LogMessageTableModel model = (LogMessageTableModel) logTable.getModel();
                    JPopupMenu popup = new JPopupMenu();

                    popup.add(new AbstractAction("Resend") {
                        @Override
                        public void actionPerformed(ActionEvent e) {
                            for (int r : logTable.getSelectedRows()) {
                                LogMessage message = model.getMessage(r);
                                if (isIoType(message)) {
                                    send(message.getCanmsg());
                                }
                            }
                        }
                    });

                    popup.add(new AbstractAction("Copy") {
                        @Override
                        public void actionPerformed(ActionEvent e) {
                            StringBuilder builder = new StringBuilder();
                            String separator = getProperty("line.separator");

                            for (int r : logTable.getSelectedRows()) {
                                LogMessage message = model.getMessage(r);
                                if (isIoType(message)) {
                                    builder.append(message.getCanmsg().toString());
                                    builder.append(separator);
                                }
                            }

                            getDefaultToolkit()
                                    .getSystemClipboard()
                                    .setContents(new StringSelection(builder.toString()), null);
                        }
                    });

                    popup.show(e.getComponent(), e.getX(), e.getY());
                }
            }
        });

        // configure table columns
        TableColumnModel columnModel = logTable.getColumnModel();
        columnModel.getColumn(0).setPreferredWidth(100);
        columnModel.getColumn(1).setPreferredWidth(40);
        columnModel.getColumn(2).setPreferredWidth(90);
        columnModel.getColumn(3).setPreferredWidth(40);
        columnModel.getColumn(4).setPreferredWidth(370);
        
        // set alignment of id column
        DefaultTableCellRenderer rightRenderer = new DefaultTableCellRenderer();
        rightRenderer.setHorizontalAlignment(DefaultTableCellRenderer.RIGHT);
        columnModel.getColumn(2).setCellRenderer(rightRenderer);

        // set alignment of length column
        DefaultTableCellRenderer centerRenderer = new DefaultTableCellRenderer();
        centerRenderer.setHorizontalAlignment(DefaultTableCellRenderer.CENTER);
        columnModel.getColumn(3).setCellRenderer(centerRenderer);

        // monitor table
        columnModel = monitorTable.getColumnModel();
        columnModel.getColumn(0).setPreferredWidth(50);
        columnModel.getColumn(1).setPreferredWidth(50);
        columnModel.getColumn(2).setPreferredWidth(40);
        columnModel.getColumn(3).setPreferredWidth(90);
        columnModel.getColumn(4).setPreferredWidth(40);
        columnModel.getColumn(5).setPreferredWidth(370);        
        columnModel.getColumn(3).setCellRenderer(rightRenderer);
        columnModel.getColumn(4).setCellRenderer(centerRenderer);
        
        // trigger initial sync between message string and message input fields
        msgString2msgFields();
        
        // init message listener
        usbtin.addMessageListener(this);
    }

    /**
     * This method is called from within the constructor to initialize the form.
     * WARNING: Do NOT modify this code. The content of this method is always
     * regenerated by the Form Editor.
     */
    @SuppressWarnings("unchecked")
    // <editor-fold defaultstate="collapsed" desc="Generated Code">//GEN-BEGIN:initComponents
    private void initComponents() {

        serialPort = new javax.swing.JComboBox();
        bitRate = new javax.swing.JComboBox();
        connectionButton = new javax.swing.JButton();
        sendMessage = new javax.swing.JTextField();
        sendButton = new javax.swing.JButton();
        followButton = new javax.swing.JToggleButton();
        openmodeComboBox = new javax.swing.JComboBox();
        clearButton = new javax.swing.JButton();
        msgId = new javax.swing.JTextField();
        msgLength = new javax.swing.JSpinner();
        msgData0 = new javax.swing.JTextField();
        msgData1 = new javax.swing.JTextField();
        msgData2 = new javax.swing.JTextField();
        msgData3 = new javax.swing.JTextField();
        msgData4 = new javax.swing.JTextField();
        msgData5 = new javax.swing.JTextField();
        msgData6 = new javax.swing.JTextField();
        msgData7 = new javax.swing.JTextField();
        msgExt = new javax.swing.JCheckBox();
        msgRTR = new javax.swing.JCheckBox();
        mainTabbedPane = new javax.swing.JTabbedPane();
        logScrollPane = new javax.swing.JScrollPane();
        logTable = new javax.swing.JTable();
        monitorScrollPane = new javax.swing.JScrollPane();
        monitorTable = new javax.swing.JTable();
        filterScrollPane = new javax.swing.JScrollPane();
        jPanel1 = new javax.swing.JPanel();
        filtersEnabled = new javax.swing.JCheckBox();
        jSeparator1 = new javax.swing.JSeparator();
        jPanel2 = new javax.swing.JPanel();
        sendFilters = new javax.swing.JButton();
        jLabel1 = new javax.swing.JLabel();

        setDefaultCloseOperation(javax.swing.WindowConstants.EXIT_ON_CLOSE);
        setTitle("UCCBViewer");

        serialPort.setEditable(true);
        serialPort.setModel(new javax.swing.DefaultComboBoxModel(SerialPortList.getPortNames()));
        serialPort.setToolTipText("Port");

        bitRate.setEditable(true);
        bitRate.setModel(new javax.swing.DefaultComboBoxModel(new String[] { "10000", "20000", "50000", "100000", "125000", "250000", "500000", "800000", "1000000" }));
        bitRate.setToolTipText("Baudrate");

        connectionButton.setText("Connect");
        connectionButton.addActionListener(new java.awt.event.ActionListener() {
            public void actionPerformed(java.awt.event.ActionEvent evt) {
                connectionButtonActionPerformed(evt);
            }
        });

        sendMessage.setText("t00181122334455667788");
        sendMessage.addActionListener(new java.awt.event.ActionListener() {
            public void actionPerformed(java.awt.event.ActionEvent evt) {
                sendMessageActionPerformed(evt);
            }
        });

        sendButton.setText("Send");
        sendButton.setEnabled(false);
        sendButton.addActionListener(new java.awt.event.ActionListener() {
            public void actionPerformed(java.awt.event.ActionEvent evt) {
                sendButtonActionPerformed(evt);
            }
        });

        followButton.setSelected(true);
        followButton.setText("Follow");

        openmodeComboBox.setModel(new DefaultComboBoxModel(USBtin.OpenMode.values()));
        openmodeComboBox.setToolTipText("Mode");

        clearButton.setText("Clear");
        clearButton.addActionListener(new java.awt.event.ActionListener() {
            public void actionPerformed(java.awt.event.ActionEvent evt) {
                clearButtonActionPerformed(evt);
            }
        });

        msgId.setColumns(8);
        msgId.setText("001");

        msgLength.setFont(new java.awt.Font("Dialog", 0, 12)); // NOI18N
        msgLength.setModel(new javax.swing.SpinnerNumberModel(0, 0, 8, 1));
        msgLength.addChangeListener(new javax.swing.event.ChangeListener() {
            public void stateChanged(javax.swing.event.ChangeEvent evt) {
                msgLengthStateChanged(evt);
            }
        });

        msgData0.setColumns(2);
        msgData0.setText("00");
        msgData0.setEnabled(false);

        msgData1.setColumns(2);
        msgData1.setText("00");
        msgData1.setEnabled(false);

        msgData2.setColumns(2);
        msgData2.setText("00");
        msgData2.setEnabled(false);

        msgData3.setColumns(2);
        msgData3.setText("00");
        msgData3.setEnabled(false);

        msgData4.setColumns(2);
        msgData4.setText("00");
        msgData4.setEnabled(false);

        msgData5.setColumns(2);
        msgData5.setText("00");
        msgData5.setEnabled(false);

        msgData6.setColumns(2);
        msgData6.setText("00");
        msgData6.setEnabled(false);

        msgData7.setColumns(2);
        msgData7.setText("00");
        msgData7.setEnabled(false);

        msgExt.setFont(new java.awt.Font("Dialog", 0, 12)); // NOI18N
        msgExt.setText("Ext");
        msgExt.addActionListener(new java.awt.event.ActionListener() {
            public void actionPerformed(java.awt.event.ActionEvent evt) {
                msgExtActionPerformed(evt);
            }
        });

        msgRTR.setFont(new java.awt.Font("Dialog", 0, 12)); // NOI18N
        msgRTR.setText("RTR");
        msgRTR.addActionListener(new java.awt.event.ActionListener() {
            public void actionPerformed(java.awt.event.ActionEvent evt) {
                msgRTRActionPerformed(evt);
            }
        });

        mainTabbedPane.setName("mainTabbedPane"); // NOI18N

        logScrollPane.setName("logTable"); // NOI18N

        logTable.setModel(new LogMessageTableModel());
        logScrollPane.setViewportView(logTable);

        mainTabbedPane.addTab("Trace", logScrollPane);

        monitorScrollPane.setName("logTable"); // NOI18N

        monitorTable.setModel(new LogMessageTableModel());
        monitorScrollPane.setViewportView(monitorTable);

        mainTabbedPane.addTab("Monitor", monitorScrollPane);

        filterScrollPane.setName("logTable"); // NOI18N

        jPanel1.setPreferredSize(new java.awt.Dimension(100, 100));

        filtersEnabled.setSelected(true);
        filtersEnabled.setText("Enable Filtering");
        filtersEnabled.setHorizontalAlignment(javax.swing.SwingConstants.LEFT);
        filtersEnabled.addItemListener(new java.awt.event.ItemListener() {
            public void itemStateChanged(java.awt.event.ItemEvent evt) {
                filtersEnabledItemStateChanged(evt);
            }
        });

        sendFilters.setText("send");
        sendFilters.addActionListener(new java.awt.event.ActionListener() {
            public void actionPerformed(java.awt.event.ActionEvent evt) {
                sendFiltersActionPerformed(evt);
            }
        });

        javax.swing.GroupLayout jPanel2Layout = new javax.swing.GroupLayout(jPanel2);
        jPanel2.setLayout(jPanel2Layout);
        jPanel2Layout.setHorizontalGroup(
            jPanel2Layout.createParallelGroup(javax.swing.GroupLayout.Alignment.LEADING)
            .addGroup(javax.swing.GroupLayout.Alignment.TRAILING, jPanel2Layout.createSequentialGroup()
                .addContainerGap(javax.swing.GroupLayout.DEFAULT_SIZE, Short.MAX_VALUE)
                .addComponent(sendFilters)
                .addContainerGap())
        );
        jPanel2Layout.setVerticalGroup(
            jPanel2Layout.createParallelGroup(javax.swing.GroupLayout.Alignment.LEADING)
            .addGroup(javax.swing.GroupLayout.Alignment.TRAILING, jPanel2Layout.createSequentialGroup()
                .addContainerGap(195, Short.MAX_VALUE)
                .addComponent(sendFilters)
                .addGap(21, 21, 21))
        );

        jLabel1.setText("Filters are in ");

        javax.swing.GroupLayout jPanel1Layout = new javax.swing.GroupLayout(jPanel1);
        jPanel1.setLayout(jPanel1Layout);
        jPanel1Layout.setHorizontalGroup(
            jPanel1Layout.createParallelGroup(javax.swing.GroupLayout.Alignment.LEADING)
            .addGroup(jPanel1Layout.createSequentialGroup()
                .addGap(18, 18, 18)
                .addComponent(filtersEnabled)
                .addPreferredGap(javax.swing.LayoutStyle.ComponentPlacement.RELATED, 216, Short.MAX_VALUE)
                .addComponent(jLabel1)
                .addGap(184, 184, 184))
            .addGroup(jPanel1Layout.createSequentialGroup()
                .addContainerGap()
                .addGroup(jPanel1Layout.createParallelGroup(javax.swing.GroupLayout.Alignment.LEADING, false)
                    .addComponent(jSeparator1, javax.swing.GroupLayout.DEFAULT_SIZE, 538, Short.MAX_VALUE)
                    .addComponent(jPanel2, javax.swing.GroupLayout.DEFAULT_SIZE, javax.swing.GroupLayout.DEFAULT_SIZE, Short.MAX_VALUE))
                .addContainerGap(javax.swing.GroupLayout.DEFAULT_SIZE, Short.MAX_VALUE))
        );
        jPanel1Layout.setVerticalGroup(
            jPanel1Layout.createParallelGroup(javax.swing.GroupLayout.Alignment.LEADING)
            .addGroup(jPanel1Layout.createSequentialGroup()
                .addContainerGap()
                .addGroup(jPanel1Layout.createParallelGroup(javax.swing.GroupLayout.Alignment.BASELINE)
                    .addComponent(filtersEnabled)
                    .addComponent(jLabel1))
                .addPreferredGap(javax.swing.LayoutStyle.ComponentPlacement.UNRELATED)
                .addComponent(jSeparator1, javax.swing.GroupLayout.PREFERRED_SIZE, 10, javax.swing.GroupLayout.PREFERRED_SIZE)
                .addPreferredGap(javax.swing.LayoutStyle.ComponentPlacement.UNRELATED)
                .addComponent(jPanel2, javax.swing.GroupLayout.PREFERRED_SIZE, javax.swing.GroupLayout.DEFAULT_SIZE, javax.swing.GroupLayout.PREFERRED_SIZE)
                .addContainerGap(37, Short.MAX_VALUE))
        );

        filterScrollPane.setViewportView(jPanel1);

        mainTabbedPane.addTab("Filter", filterScrollPane);

        javax.swing.GroupLayout layout = new javax.swing.GroupLayout(getContentPane());
        getContentPane().setLayout(layout);
        layout.setHorizontalGroup(
            layout.createParallelGroup(javax.swing.GroupLayout.Alignment.LEADING)
            .addGroup(layout.createSequentialGroup()
                .addContainerGap()
                .addGroup(layout.createParallelGroup(javax.swing.GroupLayout.Alignment.LEADING)
                    .addGroup(javax.swing.GroupLayout.Alignment.TRAILING, layout.createSequentialGroup()
                        .addGroup(layout.createParallelGroup(javax.swing.GroupLayout.Alignment.LEADING)
                            .addGroup(layout.createSequentialGroup()
                                .addComponent(msgId, javax.swing.GroupLayout.PREFERRED_SIZE, javax.swing.GroupLayout.DEFAULT_SIZE, javax.swing.GroupLayout.PREFERRED_SIZE)
                                .addPreferredGap(javax.swing.LayoutStyle.ComponentPlacement.RELATED)
                                .addComponent(msgLength, javax.swing.GroupLayout.PREFERRED_SIZE, javax.swing.GroupLayout.DEFAULT_SIZE, javax.swing.GroupLayout.PREFERRED_SIZE)
                                .addPreferredGap(javax.swing.LayoutStyle.ComponentPlacement.RELATED)
                                .addComponent(msgData0, javax.swing.GroupLayout.PREFERRED_SIZE, javax.swing.GroupLayout.DEFAULT_SIZE, javax.swing.GroupLayout.PREFERRED_SIZE)
                                .addPreferredGap(javax.swing.LayoutStyle.ComponentPlacement.RELATED)
                                .addComponent(msgData1, javax.swing.GroupLayout.PREFERRED_SIZE, javax.swing.GroupLayout.DEFAULT_SIZE, javax.swing.GroupLayout.PREFERRED_SIZE)
                                .addPreferredGap(javax.swing.LayoutStyle.ComponentPlacement.RELATED)
                                .addComponent(msgData2, javax.swing.GroupLayout.PREFERRED_SIZE, javax.swing.GroupLayout.DEFAULT_SIZE, javax.swing.GroupLayout.PREFERRED_SIZE)
                                .addPreferredGap(javax.swing.LayoutStyle.ComponentPlacement.RELATED)
                                .addComponent(msgData3, javax.swing.GroupLayout.PREFERRED_SIZE, javax.swing.GroupLayout.DEFAULT_SIZE, javax.swing.GroupLayout.PREFERRED_SIZE)
                                .addPreferredGap(javax.swing.LayoutStyle.ComponentPlacement.RELATED)
                                .addComponent(msgData4, javax.swing.GroupLayout.PREFERRED_SIZE, javax.swing.GroupLayout.DEFAULT_SIZE, javax.swing.GroupLayout.PREFERRED_SIZE)
                                .addPreferredGap(javax.swing.LayoutStyle.ComponentPlacement.RELATED)
                                .addComponent(msgData5, javax.swing.GroupLayout.PREFERRED_SIZE, javax.swing.GroupLayout.DEFAULT_SIZE, javax.swing.GroupLayout.PREFERRED_SIZE)
                                .addPreferredGap(javax.swing.LayoutStyle.ComponentPlacement.RELATED)
                                .addComponent(msgData6, javax.swing.GroupLayout.PREFERRED_SIZE, javax.swing.GroupLayout.DEFAULT_SIZE, javax.swing.GroupLayout.PREFERRED_SIZE)
                                .addPreferredGap(javax.swing.LayoutStyle.ComponentPlacement.RELATED)
                                .addComponent(msgData7, javax.swing.GroupLayout.PREFERRED_SIZE, javax.swing.GroupLayout.DEFAULT_SIZE, javax.swing.GroupLayout.PREFERRED_SIZE))
                            .addComponent(sendMessage))
                        .addGap(38, 38, 38)
                        .addComponent(msgExt)
                        .addGap(18, 18, 18)
                        .addComponent(msgRTR)
                        .addPreferredGap(javax.swing.LayoutStyle.ComponentPlacement.RELATED, javax.swing.GroupLayout.DEFAULT_SIZE, Short.MAX_VALUE)
                        .addComponent(sendButton))
                    .addGroup(layout.createSequentialGroup()
                        .addGroup(layout.createParallelGroup(javax.swing.GroupLayout.Alignment.LEADING)
                            .addComponent(mainTabbedPane, javax.swing.GroupLayout.PREFERRED_SIZE, 588, javax.swing.GroupLayout.PREFERRED_SIZE)
                            .addGroup(layout.createSequentialGroup()
                                .addComponent(serialPort, javax.swing.GroupLayout.PREFERRED_SIZE, javax.swing.GroupLayout.DEFAULT_SIZE, javax.swing.GroupLayout.PREFERRED_SIZE)
                                .addPreferredGap(javax.swing.LayoutStyle.ComponentPlacement.RELATED)
                                .addComponent(bitRate, javax.swing.GroupLayout.PREFERRED_SIZE, javax.swing.GroupLayout.DEFAULT_SIZE, javax.swing.GroupLayout.PREFERRED_SIZE)
                                .addPreferredGap(javax.swing.LayoutStyle.ComponentPlacement.RELATED)
                                .addComponent(openmodeComboBox, javax.swing.GroupLayout.PREFERRED_SIZE, javax.swing.GroupLayout.DEFAULT_SIZE, javax.swing.GroupLayout.PREFERRED_SIZE)
                                .addPreferredGap(javax.swing.LayoutStyle.ComponentPlacement.RELATED)
                                .addComponent(connectionButton)
                                .addGap(112, 112, 112)
                                .addComponent(clearButton)
                                .addPreferredGap(javax.swing.LayoutStyle.ComponentPlacement.RELATED)
                                .addComponent(followButton)))
                        .addGap(0, 0, Short.MAX_VALUE)))
                .addContainerGap())
        );
        layout.setVerticalGroup(
            layout.createParallelGroup(javax.swing.GroupLayout.Alignment.LEADING)
            .addGroup(layout.createSequentialGroup()
                .addContainerGap()
                .addGroup(layout.createParallelGroup(javax.swing.GroupLayout.Alignment.BASELINE)
                    .addComponent(serialPort, javax.swing.GroupLayout.PREFERRED_SIZE, javax.swing.GroupLayout.DEFAULT_SIZE, javax.swing.GroupLayout.PREFERRED_SIZE)
                    .addComponent(bitRate, javax.swing.GroupLayout.PREFERRED_SIZE, javax.swing.GroupLayout.DEFAULT_SIZE, javax.swing.GroupLayout.PREFERRED_SIZE)
                    .addComponent(openmodeComboBox, javax.swing.GroupLayout.PREFERRED_SIZE, javax.swing.GroupLayout.DEFAULT_SIZE, javax.swing.GroupLayout.PREFERRED_SIZE)
                    .addComponent(connectionButton)
                    .addComponent(followButton)
                    .addComponent(clearButton))
                .addPreferredGap(javax.swing.LayoutStyle.ComponentPlacement.UNRELATED)
                .addComponent(mainTabbedPane, javax.swing.GroupLayout.PREFERRED_SIZE, 363, javax.swing.GroupLayout.PREFERRED_SIZE)
                .addPreferredGap(javax.swing.LayoutStyle.ComponentPlacement.RELATED, 19, Short.MAX_VALUE)
                .addGroup(layout.createParallelGroup(javax.swing.GroupLayout.Alignment.BASELINE)
                    .addComponent(msgId, javax.swing.GroupLayout.PREFERRED_SIZE, javax.swing.GroupLayout.DEFAULT_SIZE, javax.swing.GroupLayout.PREFERRED_SIZE)
                    .addComponent(msgLength, javax.swing.GroupLayout.PREFERRED_SIZE, javax.swing.GroupLayout.DEFAULT_SIZE, javax.swing.GroupLayout.PREFERRED_SIZE)
                    .addComponent(msgData0, javax.swing.GroupLayout.PREFERRED_SIZE, javax.swing.GroupLayout.DEFAULT_SIZE, javax.swing.GroupLayout.PREFERRED_SIZE)
                    .addComponent(msgData1, javax.swing.GroupLayout.PREFERRED_SIZE, javax.swing.GroupLayout.DEFAULT_SIZE, javax.swing.GroupLayout.PREFERRED_SIZE)
                    .addComponent(msgData2, javax.swing.GroupLayout.PREFERRED_SIZE, javax.swing.GroupLayout.DEFAULT_SIZE, javax.swing.GroupLayout.PREFERRED_SIZE)
                    .addComponent(msgData3, javax.swing.GroupLayout.PREFERRED_SIZE, javax.swing.GroupLayout.DEFAULT_SIZE, javax.swing.GroupLayout.PREFERRED_SIZE)
                    .addComponent(msgData4, javax.swing.GroupLayout.PREFERRED_SIZE, javax.swing.GroupLayout.DEFAULT_SIZE, javax.swing.GroupLayout.PREFERRED_SIZE)
                    .addComponent(msgData5, javax.swing.GroupLayout.PREFERRED_SIZE, javax.swing.GroupLayout.DEFAULT_SIZE, javax.swing.GroupLayout.PREFERRED_SIZE)
                    .addComponent(msgData6, javax.swing.GroupLayout.PREFERRED_SIZE, javax.swing.GroupLayout.DEFAULT_SIZE, javax.swing.GroupLayout.PREFERRED_SIZE)
                    .addComponent(msgData7, javax.swing.GroupLayout.PREFERRED_SIZE, javax.swing.GroupLayout.DEFAULT_SIZE, javax.swing.GroupLayout.PREFERRED_SIZE)
                    .addComponent(msgExt)
                    .addComponent(msgRTR))
                .addPreferredGap(javax.swing.LayoutStyle.ComponentPlacement.RELATED)
                .addGroup(layout.createParallelGroup(javax.swing.GroupLayout.Alignment.BASELINE)
                    .addComponent(sendMessage, javax.swing.GroupLayout.PREFERRED_SIZE, javax.swing.GroupLayout.DEFAULT_SIZE, javax.swing.GroupLayout.PREFERRED_SIZE)
                    .addComponent(sendButton))
                .addContainerGap())
        );

        mainTabbedPane.getAccessibleContext().setAccessibleName("");

        pack();
    }// </editor-fold>//GEN-END:initComponents

    /**
     * Handle connect/disconnect button action
     * @param evt Action event
     */
    private void connectionButtonActionPerformed(java.awt.event.ActionEvent evt) {//GEN-FIRST:event_connectionButtonActionPerformed

        if (connectionButton.getText().equals("Disconnect")) {
            try {
                usbtin.closeCANChannel();
                usbtin.disconnect();
                log("Disconnected", LogMessage.MessageType.INFO);
            } catch (USBtinException e) {
                log(e.getMessage(), LogMessage.MessageType.ERROR);
            }
            connectionButton.setText("Connect");
            bitRate.setEnabled(true);
            serialPort.setEnabled(true);
            sendButton.setEnabled(false);
            openmodeComboBox.setEnabled(true);
        } else {
            try {
                usbtin.connect((String) serialPort.getSelectedItem());
                usbtin.openCANChannel(Integer.parseInt((String) bitRate.getSelectedItem()), (USBtin.OpenMode) openmodeComboBox.getSelectedItem());
                connectionButton.setText("Disconnect");
                bitRate.setEnabled(false);
                serialPort.setEnabled(false);
                sendButton.setEnabled(true);
                openmodeComboBox.setEnabled(false);
                log("Connected to USBtin (FW" + usbtin.getFirmwareVersion() + "/HW" + usbtin.getHardwareVersion() + ", SN: " + usbtin.getSerialNumber() + ")", LogMessage.MessageType.INFO);

                if (baseTimestamp == 0) {
                    baseTimestamp = System.currentTimeMillis();
                }
            } catch (USBtinException e) {
                log(e.getMessage(), LogMessage.MessageType.ERROR);
            }
        }
    }//GEN-LAST:event_connectionButtonActionPerformed

    /**
     * Handle send button event
     * 
     * @param evt Action event
     */
    private void sendButtonActionPerformed(java.awt.event.ActionEvent evt) {//GEN-FIRST:event_sendButtonActionPerformed
        send();
    }//GEN-LAST:event_sendButtonActionPerformed

    /**
     * Handle clear button event
     * 
     * @param evt Action event
     */
    private void clearButtonActionPerformed(java.awt.event.ActionEvent evt) {//GEN-FIRST:event_clearButtonActionPerformed
        LogMessageTableModel tm = (LogMessageTableModel) logTable.getModel();
        tm.clear();
        baseTimestamp = System.currentTimeMillis();
        
        MonitorMessageTableModel mtm = (MonitorMessageTableModel) monitorTable.getModel();
        mtm.clear();
    }//GEN-LAST:event_clearButtonActionPerformed

    /**
     * Handle message length input field change event
     * 
     * @param evt Change event
     */
    private void msgLengthStateChanged(javax.swing.event.ChangeEvent evt) {//GEN-FIRST:event_msgLengthStateChanged
        msgFields2msgString();
    }//GEN-LAST:event_msgLengthStateChanged

    /**
     * Handle message extended checkbox event
     * 
     * @param evt Action event
     */
    private void msgExtActionPerformed(java.awt.event.ActionEvent evt) {//GEN-FIRST:event_msgExtActionPerformed
        msgFields2msgString();
    }//GEN-LAST:event_msgExtActionPerformed

    /**
     * Handle message RTR checkbox event
     * 
     * @param evt Action event
     */
    private void msgRTRActionPerformed(java.awt.event.ActionEvent evt) {//GEN-FIRST:event_msgRTRActionPerformed
        msgFields2msgString();
    }//GEN-LAST:event_msgRTRActionPerformed

    /**
     * Handle message string action event
     * 
     * @param evt Action event
     */
    private void sendMessageActionPerformed(java.awt.event.ActionEvent evt) {//GEN-FIRST:event_sendMessageActionPerformed
        if (sendButton.isEnabled()) {
            send();
        }
    }//GEN-LAST:event_sendMessageActionPerformed
    
    /**
     * Send filters
     * 
     * @throws USBtinException On serial port errors
     */
    private void sendFiltersActionPerformed(java.awt.event.ActionEvent evt) {//GEN-FIRST:event_sendFiltersActionPerformed
            
            int fs = 0;
            for (int i = 0; i != filterTextFields.length; i++)
             {
                if (filterCheckBoxs[i].isSelected())
                {
                    int filter = (int)Long.parseUnsignedLong(filterTextFields[i].getText(), 16);
                    try {
                        fs ++;
                        usbtin.writeFilter(new uCCBlib.FilterChain(i,i,1,1,1,0,filter,0,0,filter,0,0));                    
                    } catch (USBtinException ex) {
                        Logger.getLogger(USBtinViewer.class.getName()).log(Level.SEVERE, null, ex);
                    }
                }
             }
            if (fs > 0)
                log(new LogMessage(null, fs+" Filters send", LogMessage.MessageType.INFO, System.currentTimeMillis() - baseTimestamp));
            else 
                log(new LogMessage(null, "No Filters send", LogMessage.MessageType.ERROR, System.currentTimeMillis() - baseTimestamp));
    }//GEN-LAST:event_sendFiltersActionPerformed

    private void filtersEnabledItemStateChanged(java.awt.event.ItemEvent evt) {//GEN-FIRST:event_filtersEnabledItemStateChanged
                jPanel2.setEnabled(false);        // TODO add your handling code here:
                
                boolean state = filtersEnabled.isSelected();
                
                sendFilters.setEnabled(state);
                for (int i = 0; i != filterTextFields.length; i++)
                {
                    filterTextFields[i].setEnabled(state);
                    filterCheckBoxs[i].setEnabled(state);
                }
                
                if (state == false)
                {
                    try {
                        usbtin.writeFilter(null);
                     } catch (USBtinException ex) {
                        Logger.getLogger(USBtinViewer.class.getName()).log(Level.SEVERE, null, ex);
                    }
                    log(new LogMessage(null, "Filtering disabled", LogMessage.MessageType.INFO, System.currentTimeMillis() - baseTimestamp));
                }          
    }//GEN-LAST:event_filtersEnabledItemStateChanged

    JTextField[] filterTextFields;
    JCheckBox[]  filterCheckBoxs;
    void CreateFiltersArray(JPanel p, int n)
    {
        n = 13;
        
        filterTextFields = new JTextField[n];
        filterCheckBoxs = new JCheckBox[n];
        
        
        int oX = 30;
        int oY = 10;
        
        int rC = 5;
                
        for (int i =0; i != n; i++)
        {
            filterTextFields[i] = new JTextField();
            filterCheckBoxs[i] = new JCheckBox();
            
            filterTextFields[i].setBounds(oX + 30 + 190 * (int)(i/rC) , oY + 50 *(i%rC), 100, 25);
            filterCheckBoxs[i].setBounds(oX + 190 * (int)(i/rC) ,  oY + 50 *(i%rC), 20, 20);
//           
            p.add(filterTextFields[i]);
            p.add(filterCheckBoxs[i]);
            
        }
    }
    
    /**
     * Entry point of USBtinViewer application
     * 
     * @param args The command line arguments
     */
    public static void main(String args[]) {
        /* Set the Nimbus look and feel */
        //<editor-fold defaultstate="collapsed" desc=" Look and feel setting code (optional) ">
        /* If Nimbus (introduced in Java SE 6) is not available, stay with the default look and feel.
         * For details see http://download.oracle.com/javase/tutorial/uiswing/lookandfeel/plaf.html 
         */
        try {
            for (javax.swing.UIManager.LookAndFeelInfo info : javax.swing.UIManager.getInstalledLookAndFeels()) {
                if ("Nimbus".equals(info.getName())) {
                    javax.swing.UIManager.setLookAndFeel(info.getClassName());
                    break;
                }
            }
        } catch (ClassNotFoundException ex) {
            java.util.logging.Logger.getLogger(USBtinViewer.class.getName()).log(java.util.logging.Level.SEVERE, null, ex);
        } catch (InstantiationException ex) {
            java.util.logging.Logger.getLogger(USBtinViewer.class.getName()).log(java.util.logging.Level.SEVERE, null, ex);
        } catch (IllegalAccessException ex) {
            java.util.logging.Logger.getLogger(USBtinViewer.class.getName()).log(java.util.logging.Level.SEVERE, null, ex);
        } catch (javax.swing.UnsupportedLookAndFeelException ex) {
            java.util.logging.Logger.getLogger(USBtinViewer.class.getName()).log(java.util.logging.Level.SEVERE, null, ex);
        }
        //</editor-fold>
        //</editor-fold>

        /* Create and display the form */
        java.awt.EventQueue.invokeLater(new Runnable() {
            @Override
            public void run() {      
                new USBtinViewer().setVisible(true);
            }
        });
    }

    // Variables declaration - do not modify//GEN-BEGIN:variables
    private javax.swing.JComboBox bitRate;
    private javax.swing.JButton clearButton;
    private javax.swing.JButton connectionButton;
    private javax.swing.JScrollPane filterScrollPane;
    private javax.swing.JCheckBox filtersEnabled;
    private javax.swing.JToggleButton followButton;
    private javax.swing.JLabel jLabel1;
    private javax.swing.JPanel jPanel1;
    private javax.swing.JPanel jPanel2;
    private javax.swing.JSeparator jSeparator1;
    private javax.swing.JScrollPane logScrollPane;
    private javax.swing.JTable logTable;
    private javax.swing.JTabbedPane mainTabbedPane;
    private javax.swing.JScrollPane monitorScrollPane;
    private javax.swing.JTable monitorTable;
    private javax.swing.JTextField msgData0;
    private javax.swing.JTextField msgData1;
    private javax.swing.JTextField msgData2;
    private javax.swing.JTextField msgData3;
    private javax.swing.JTextField msgData4;
    private javax.swing.JTextField msgData5;
    private javax.swing.JTextField msgData6;
    private javax.swing.JTextField msgData7;
    private javax.swing.JCheckBox msgExt;
    private javax.swing.JTextField msgId;
    private javax.swing.JSpinner msgLength;
    private javax.swing.JCheckBox msgRTR;
    private javax.swing.JComboBox openmodeComboBox;
    private javax.swing.JButton sendButton;
    private javax.swing.JButton sendFilters;
    private javax.swing.JTextField sendMessage;
    private javax.swing.JComboBox serialPort;
    // End of variables declaration//GEN-END:variables

    /**
     * Receive can message (called via listener)
     * 
     * @param canmsg CAN message
     */
    @Override
    public void receiveCANMessage(CANMessage canmsg) {
        log(new LogMessage(canmsg, null, LogMessage.MessageType.IN, System.currentTimeMillis() - baseTimestamp));
    }

    /**
     * Insert given message to log list
     * 
     * @param message Message to insert
     */
    public void log(LogMessage message) {
        LogMessageTableModel tm = (LogMessageTableModel) logTable.getModel();
        tm.addMessage(message);
        if (followButton.isSelected()) {
            logTable.scrollRectToVisible(logTable.getCellRect(tm.getRowCount() - 1, 0, true));
        }

        if ((message.type == LogMessage.MessageType.OUT) ||
                (message.type == LogMessage.MessageType.IN)) {
            MonitorMessageTableModel mtm = (MonitorMessageTableModel) monitorTable.getModel();
            mtm.add(message);
        }

    }

    /**
     * Insert message with given typte to log list
     * 
     * @param msg Message to insert
     * @param type Type of message
     */
    public void log(String msg, LogMessage.MessageType type) {
        LogMessageTableModel tm = (LogMessageTableModel) logTable.getModel();
        tm.addMessage(new LogMessage(null, msg, type, System.currentTimeMillis() - baseTimestamp));
        if (followButton.isSelected()) {
            logTable.scrollRectToVisible(logTable.getCellRect(tm.getRowCount() - 1, 0, true));
        }        
    }

    /**
     * Send out CAN message string
     */
    public void send() {
        send(new CANMessage(sendMessage.getText()));
    }

    /**
     * Send out a CAN message
     */
    public void send(CANMessage canmsg) {
        log(new LogMessage(canmsg, null, LogMessage.MessageType.OUT, System.currentTimeMillis() - baseTimestamp));
        try {
            usbtin.send(canmsg);
        } catch (USBtinException e) {
            log(e.getMessage(), LogMessage.MessageType.ERROR);
        }
    }

    /**
     * Convert message input fields to string representation
     */
    protected void msgFields2msgString() {
        if (disableMsgUpdate) {
            return;
        }
        disableMsgUpdate = true;

        int id;
        try {
            id = Integer.parseInt(msgId.getText(), 16);
        } catch (java.lang.NumberFormatException e) {
            id = 0;
        }

        // if id is greater than standard 11bit, set extended flag
        if (id > 0x7ff) {
            msgExt.setSelected(true);
        }

        int length = (Integer) msgLength.getValue();
        byte[] data = new byte[length];
        for (int i = 0; i < length; i++) {
            try {
                data[i] = (byte) Integer.parseInt(msgDataFields[i].getText(), 16);
            } catch (java.lang.NumberFormatException e) {
                data[i] = 0;
            }

        }
        CANMessage canmsg = new CANMessage(id, data, msgExt.isSelected(), msgRTR.isSelected());
        sendMessage.setText(canmsg.toString());

        for (int i = 0; i < msgDataFields.length; i++) {
            msgDataFields[i].setEnabled(i < (Integer) msgLength.getValue() && !msgRTR.isSelected());
        }

        disableMsgUpdate = false;
    }

    /**
     * Convert string representation of message to input fields
     */
    protected void msgString2msgFields() {
        if (disableMsgUpdate) {
            return;
        }
        disableMsgUpdate = true;

        CANMessage canmsg = new CANMessage(sendMessage.getText());
        byte[] data = canmsg.getData();

        if (canmsg.isExtended()) {
            msgId.setText(String.format("%08x", canmsg.getId()));
        } else {
            msgId.setText(String.format("%03x", canmsg.getId()));
        }

        msgLength.setValue(data.length);

        msgRTR.setSelected(canmsg.isRtr());
        msgExt.setSelected(canmsg.isExtended());

        for (int i = 0; i < msgDataFields.length; i++) {
            if (i < data.length) {
                msgDataFields[i].setText(String.format("%02x", data[i]));
            }
            msgDataFields[i].setEnabled(i < (Integer) msgLength.getValue() && !msgRTR.isSelected());
        }

        disableMsgUpdate = false;
    }
}
